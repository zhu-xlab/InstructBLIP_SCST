"""
 Copyright (c) 2022, salesforce.com, inc.
 All rights reserved.
 SPDX-License-Identifier: BSD-3-Clause
 For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
"""

# scst_implementation.py

import logging
import json
import os
import copy
import random
from typing import Dict, List, Any, Tuple

import torch
import torch.nn.functional as F
import torch.distributed as dist
from torch.utils.tensorboard import SummaryWriter
from torchvision.transforms import ToPILImage

from lavis.common.dist_utils import get_rank, get_world_size, is_main_process, is_dist_avail_and_initialized
from lavis.common.logger import MetricLogger, SmoothedValue
from lavis.common.registry import registry
from lavis.datasets.data_utils import prepare_sample

from transformers import T5TokenizerFast
from lavis.models.blip2_models.modeling_t5 import T5Config, T5ForConditionalGeneration

import numpy as np
from scipy.stats import pearsonr
import deepspeed
from tqdm import tqdm

# Import from our custom reward and evolutionary files
from lavis.tasks.rewards import RewardRegistry
from lavis.tasks.evolutionary_algorithm import EvolutionaryAlgorithm

to_pil = ToPILImage()
writer = SummaryWriter('runs/scst_training')

class BaseTask:
    def __init__(self, **kwargs):
        super().__init__()

        self.min_scores = {name: None for name in RewardRegistry.registry.keys()}
        self.max_scores = {name: None for name in RewardRegistry.registry.keys()}

        self.inst_id_key = "instance_id"
        self.running_mean_reward = 0
        self.old_action_probs = None
        with open("/mnt/SSD2/thomas/LAVIS/train2.json", "r") as file:
            coco_data = json.load(file)

        image_id_to_captions = {}
        for annotation in coco_data["annotations"]:
            image_id = annotation["image_id"]
            caption = annotation["caption"]
                                
            if image_id not in image_id_to_captions:
                image_id_to_captions[image_id] = set()
                                
            image_id_to_captions[image_id].add(caption)
        self.dict_ids = image_id_to_captions

        # Initialize EvolutionaryAlgorithm
        reward_component_names = list(RewardRegistry.registry.keys())

        # Initialize tokenizer
        self.tokenizer = T5TokenizerFast.from_pretrained('google/flan-t5-xl', truncation_side='right')

        # Ev. Algorithm, rewards
        reward_component_names = list(RewardRegistry.registry.keys())
        self.reward_weights = {name: 1.0 / len(reward_component_names) for name in reward_component_names}

        # Alignment
        self.reward_correlation_sums = {name: 0.0 for name in RewardRegistry.registry.keys()}
        self.reward_correlation_counts = {name: 0 for name in RewardRegistry.registry.keys()}

    @classmethod
    def setup_task(cls, **kwargs):
        return cls()

    @classmethod
    def normalize_score(self, name, raw_scores):
        raw_scores_np = raw_scores.detach().cpu().numpy()

        # Update min and max scores dynamically
        if self.min_scores[name] is None or raw_scores_np.min() < self.min_scores[name]:
            self.min_scores[name] = raw_scores_np.min()
        if self.max_scores[name] is None or raw_scores_np.max() > self.max_scores[name]:
            self.max_scores[name] = raw_scores_np.max()

        min_score = self.min_scores[name]
        max_score = self.max_scores[name]

        # Avoid division by zero and normalize
        if max_score > min_score:
            normalized = (raw_scores_np - min_score) / (max_score - min_score)
        else:
            normalized = np.zeros_like(raw_scores_np)

        return torch.tensor(normalized, device=raw_scores.device)

    def build_model(self, cfg: Any) -> Any:
        """
        Build the model based on the configuration.

        Args:
            cfg (Any): Configuration object.

        Returns:
            Any: The constructed model.
        """
        model_config = cfg.model_cfg
        model_cls = registry.get_model_class(model_config.arch)
        return model_cls.from_config(model_config)

    def ablation_study(self, model, data_loader, reward_component_names, optimizer, lr_scheduler, scaler, cfg, epoch_count=3):
        """
        Conduct an ablation study by disabling one reward at a time.
        
        Args:
            model: The model being trained.
            data_loader: The data loader for training.
            reward_component_names: List of reward function names.
            optimizer: Optimizer for training.
            lr_scheduler: Learning rate scheduler.
            scaler: Gradient scaler for mixed precision training.
            cfg: Configuration object.
            epoch_count: Number of epochs for each ablation experiment.
        """
        results = {}
        for excluded_reward in reward_component_names:
            print(f"\nStarting ablation for reward: {excluded_reward}")
            self.reward_weights = {name: 1.0 / (len(reward_component_names) - 1) if name != excluded_reward else 0.0
                                for name in reward_component_names}
            
            # Train for a few epochs with the excluded reward
            for epoch in range(epoch_count):
                train_stats = self.train_epoch(
                    epoch=epoch,
                    model=model,
                    data_loader=data_loader,
                    optimizer=optimizer,
                    lr_scheduler=lr_scheduler,
                    scaler=scaler,
                    cuda_enabled=cfg.run_cfg.cuda_enabled,
                    log_freq=cfg.run_cfg.log_freq,
                    accum_grad_iters=cfg.run_cfg.accum_grad_iters,
                )
                # Log results for the excluded reward
                if excluded_reward not in results:
                    results[excluded_reward] = []
                results[excluded_reward].append(train_stats)
            
            # Save results to file
            os.makedirs(cfg.output_dir, exist_ok=True)
            with open(os.path.join(cfg.output_dir, f"ablation_results_{excluded_reward}.json"), "w") as f:
                json.dump(results[excluded_reward], f, indent=4)
            
            print(f"Completed ablation for reward: {excluded_reward}")
        
        print("Ablation study complete.")

    def single_reward_experiments(self, model, data_loader, reward_component_names, optimizer, lr_scheduler, scaler, cfg, epoch_count=3):
        """
        Train the model using only one reward function at a time.
        
        Args:
            model: The model being trained.
            data_loader: The data loader for training.
            reward_component_names: List of reward function names.
            optimizer: Optimizer for training.
            lr_scheduler: Learning rate scheduler.
            scaler: Gradient scaler for mixed precision training.
            cfg: Configuration object.
            epoch_count: Number of epochs for each reward experiment.
        """
        results = {}
        for single_reward in reward_component_names:
            print(f"\nStarting single-reward training for: {single_reward}")
            self.reward_weights = {name: 1.0 if name == single_reward else 0.0 for name in reward_component_names}
            
            # Train for a few epochs with only the selected reward
            for epoch in range(epoch_count):
                train_stats = self.train_epoch(
                    epoch=epoch,
                    model=model,
                    data_loader=data_loader,
                    optimizer=optimizer,
                    lr_scheduler=lr_scheduler,
                    scaler=scaler,
                    cuda_enabled=cfg.run_cfg.cuda_enabled,
                    log_freq=cfg.run_cfg.log_freq,
                    accum_grad_iters=cfg.run_cfg.accum_grad_iters,
                )
                # Log results for the single reward
                if single_reward not in results:
                    results[single_reward] = []
                results[single_reward].append(train_stats)
            
            # Save results to file
            os.makedirs(cfg.output_dir, exist_ok=True)
            with open(os.path.join(cfg.output_dir, f"single_reward_results_{single_reward}.json"), "w") as f:
                json.dump(results[single_reward], f, indent=4)
            
            print(f"Completed single-reward training for: {single_reward}")
        
        print("Single reward experiments complete.")

    def build_datasets(self, cfg: Any) -> Dict[str, Any]:
        """
        Build datasets based on the configuration.

        Args:
            cfg (Any): Configuration object.

        Returns:
            Dict[str, Any]: Dictionary of datasets.
        """
        datasets = {}
        datasets_config = cfg.datasets_cfg

        assert len(datasets_config) > 0, "At least one dataset has to be specified."

        for name in datasets_config:
            dataset_config = datasets_config[name]
            builder = registry.get_builder_class(name)(dataset_config)
            dataset = builder.build_datasets()
            datasets[name] = dataset

        return datasets

    def pearson_correlation(self, x, y):
        x_mean = torch.mean(x)
        y_mean = torch.mean(y)
        covariance = torch.sum((x - x_mean) * (y - y_mean))
        x_std = torch.sqrt(torch.sum((x - x_mean) ** 2))
        y_std = torch.sqrt(torch.sum((y - y_mean) ** 2))
        return (covariance / (x_std * y_std)).item()

    def log_reward_correlations(self, individual_rewards, composite_reward):
        composite_reward_np = composite_reward.detach().cpu().numpy()
        for name, reward_tensor in individual_rewards.items():
            reward_np = reward_tensor.detach().cpu().numpy()

            if np.var(reward_np) == 0 or np.var(composite_reward_np) == 0:
                print(f"Skipping correlation for {name} due to no variance.")
                correlation = 0.0
            else:
                correlation, _ = pearsonr(reward_np, composite_reward_np)

            self.reward_correlation_sums[name] += correlation
            self.reward_correlation_counts[name] += 1

    def get_average_correlations(self, epoch: int, output_dir: str = "./correlation_logs"):
        """
        Calculate and save the average correlation for each reward function.

        Args:
            epoch (int): Current epoch number.
            output_dir (str): Directory to save the correlation logs.
        """
        avg_correlations = {}
        for name, total_corr in self.reward_correlation_sums.items():
            count = self.reward_correlation_counts[name]
            avg_correlations[name] = total_corr / count if count > 0 else 0.0

        # Save correlations to a file
        os.makedirs(output_dir, exist_ok=True)
        log_file = os.path.join(output_dir, f"correlation_epoch_{epoch}.json")

        with open(log_file, "w") as f:
            json.dump(avg_correlations, f, indent=4)

        return avg_correlations

    def train_step(self, model, samples):
        output = model(samples)
        loss_dict = {}
        for k,v in output.items():
            if "loss" in k:
                loss_dict[k] = v
        return output["loss"], loss_dict

    def train_rl_step(self, model: Any, samples: Dict[str, Any]) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """
        Perform a single training step with added entropy for exploration and running mean reward.

        Args:
            model (Any): The model being trained.
            samples (Dict[str, Any]): Batch of samples.

        Returns:
            Tuple[torch.Tensor, Dict[str, torch.Tensor]]: Computed loss and loss dictionary.
        """
        output = model(samples)
        logits = output['logits']

        # Sample captions using torch.multinomial
        probs = F.softmax(logits, dim=-1)
        sampled_ids = torch.multinomial(probs.view(-1, probs.size(-1)), 1).view(probs.size(0), -1)

        greedy_ids = logits.argmax(dim=-1)

        # Decode captions
        sampled_captions = self.decode_captions(sampled_ids)
        greedy_captions = self.decode_captions(greedy_ids)
        ground_truth_captions = samples['text_output']

        # Compute rewards
        sampled_reward = self.compute_reward(sampled_captions, ground_truth_captions)
        greedy_reward = self.compute_reward(greedy_captions, ground_truth_captions)

        # Ensure rewards are 2D tensors
        if sampled_reward.dim() == 1:
            sampled_reward = sampled_reward.unsqueeze(1)
        if greedy_reward.dim() == 1:
            greedy_reward = greedy_reward.unsqueeze(1)

        # Ensure rewards have the same size
        max_len = max(sampled_reward.size(1), greedy_reward.size(1))
        sampled_reward = F.pad(sampled_reward, (0, max_len - sampled_reward.size(1)))
        greedy_reward = F.pad(greedy_reward, (0, max_len - greedy_reward.size(1)))

        # Compute advantage using running mean
        rewards = sampled_reward - greedy_reward
        alpha = 0.95  # Adjust this value to control the update rate of the running mean
        self.running_mean_reward = (1 - alpha) * self.running_mean_reward + alpha * rewards.mean().item()
        
        advantage = rewards - self.running_mean_reward
        advantage = (advantage - advantage.mean()) / (advantage.std() + 1e-8)

        # Compute loss
        log_probs = F.log_softmax(logits, dim=-1)

        # Ensure all tensors have the same sequence length
        min_len = min(log_probs.size(1), sampled_ids.size(1), advantage.size(1))
        log_probs = log_probs[:, :min_len, :]
        sampled_ids = sampled_ids[:, :min_len]
        advantage = advantage[:, :min_len]

        # Compute policy loss
        policy_loss = -(advantage.unsqueeze(-1) * log_probs.gather(2, sampled_ids.unsqueeze(-1))).mean()

        # Compute entropy
        entropy_per_token = -(probs * log_probs).sum(dim=-1)
        entropy = entropy_per_token.mean()

        # Compute varentropy
        varentropy = entropy_per_token.var()

        # Add entropy and varentropy to the loss with coefficients
        ##entropy_coeff = 0  # Adjust this coefficient as needed
        ##varentropy_coeff = 0  # Adjust this coefficient as needed
        ##loss = policy_loss - entropy_coeff * entropy - varentropy_coeff * varentropy
        ratio_coef = 1e-4
        loss = policy_loss + ratio_coef * varentropy / (entropy + 1e-7)

        loss_dict = {
            "loss": loss,
            "policy_loss": policy_loss,
            "entropy": entropy,
            "varentropy": varentropy,
            "mean_reward": self.running_mean_reward,
        }
        return loss, loss_dict

    def sample_captions(self, logits: torch.Tensor) -> torch.Tensor:
        """
        Sample captions from logits.

        Args:
            logits (torch.Tensor): Prediction logits from the model.

        Returns:
            torch.Tensor: Sampled caption ids.
        """
        probs = torch.nn.functional.softmax(logits, dim=-1)
        return torch.multinomial(probs.view(-1, probs.size(-1)), 1).view(probs.size(0), -1)

    def decode_captions(self, caption_ids: torch.Tensor) -> List[str]:
        """
        Decode caption ids to strings.

        Args:
            caption_ids (torch.Tensor): Tensor of caption ids.

        Returns:
            List[str]: List of decoded captions.
        """
        # Assuming you have a tokenizer attribute
        if not hasattr(self, 'tokenizer'):
            raise AttributeError("Tokenizer not initialized. Please ensure the tokenizer is properly set up in the task.")
        return self.tokenizer.batch_decode(caption_ids, skip_special_tokens=True)

    def set_reward_weights(self, weights):
        self.reward_weights = weights

    def compute_reward(self, generated_captions: List[str], reference_captions: List[str]) -> torch.Tensor:
        """
        Compute the composite reward and individual rewards.

        Args:
            generated_captions (List[str]): List of generated captions.
            reference_captions (List[str]): List of reference captions.

        Returns:
            torch.Tensor: Composite rewards.
        """
        device = "cuda" if torch.cuda.is_available() else "cpu"
        total_rewards = torch.zeros(len(generated_captions), device=device).clone()
        individual_rewards = {}

        for name, func in RewardRegistry.registry.items():
            raw_scores = func(generated_captions, reference_captions, None)
            normalized_scores = RewardRegistry.normalize_score(name, raw_scores) #self.normalize_score(name, raw_scores) #
            normalized_scores = normalized_scores.to(device)  # Ensure normalized_scores is on the same device
            individual_rewards[name] = normalized_scores
            total_rewards += RewardRegistry.weights[name] * normalized_scores

        self.log_reward_correlations(individual_rewards, total_rewards)
        return total_rewards


    def valid_step(self, model: Any, samples: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Perform a single validation step.

        Args:
            model (Any): The model being validated.
            samples (Dict[str, Any]): Batch of samples.

        Returns:
            List[Dict[str, Any]]: List of results for each sample.
        """
        results = []
        captions = model.generate(samples)
        
        for caption, sample in zip(captions, samples):
            results.append({
                "caption": caption,
                "image_id": sample["image_id"],
            })
        
        return results

    def after_evaluation(self, **kwargs):
        pass

    def before_evaluation(self, model: Any, dataset: Any, **kwargs):
        model.before_evaluation(dataset=dataset, task_type=type(self))

    def evaluation(self, model: Any, data_loader: Any, cuda_enabled: bool = True) -> List[Dict[str, Any]]:
        """
        Perform evaluation on the given data loader.

        Args:
            model (Any): The model to evaluate.
            data_loader (Any): The data loader for evaluation data.
            cuda_enabled (bool): Whether to use CUDA. Defaults to True.

        Returns:
            List[Dict[str, Any]]: List of evaluation results.
        """
        metric_logger = MetricLogger(delimiter="  ")
        header = "Evaluation"
        print_freq = 10

        results = []

        for i, samples in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
            samples = prepare_sample(samples, cuda_enabled=cuda_enabled)
            eval_output = self.valid_step(model=model, samples=samples)
            results.extend(eval_output)

        if is_dist_avail_and_initialized():
            dist.barrier()

        return results

    def train_epoch(
        self,
        epoch: int,
        model: Any,
        data_loader: Any,
        optimizer: torch.optim.Optimizer,
        lr_scheduler: Any,
        scaler: Any = None,
        cuda_enabled: bool = False,
        log_freq: int = 50,
        accum_grad_iters: int = 1,
    ) -> Dict[str, float]:
        """
        Train for one epoch.

        Args:
            epoch (int): The current epoch number.
            model (Any): The model to train.
            data_loader (Any): The data loader for training data.
            optimizer (torch.optim.Optimizer): The optimizer.
            lr_scheduler (Any): The learning rate scheduler.
            scaler (Any, optional): Gradient scaler for mixed precision training.
            cuda_enabled (bool): Whether to use CUDA. Defaults to False.
            log_freq (int): Logging frequency. Defaults to 50.
            accum_grad_iters (int): Number of iterations to accumulate gradients. Defaults to 1.

        Returns:
            Dict[str, float]: Dictionary of averaged training stats.
        """
        avg_correlations = self.get_average_correlations(epoch=epoch, output_dir="./correlation_logs")
        for name, avg_corr in avg_correlations.items():
            print(f"Average correlation for {name}: {avg_corr:.4f}")

        # Reset correlation accumulators for the next epoch
        self.reward_correlation_sums = {name: 0.0 for name in RewardRegistry.registry.keys()}
        self.reward_correlation_counts = {name: 0 for name in RewardRegistry.registry.keys()}

        return self._train_inner_loop(
            epoch=epoch,
            iters_per_epoch=len(data_loader),
            model=model,
            data_loader=data_loader,
            optimizer=optimizer,
            scaler=scaler,
            lr_scheduler=lr_scheduler,
            log_freq=log_freq,
            cuda_enabled=cuda_enabled,
            accum_grad_iters=accum_grad_iters,
        )

    def _train_inner_loop(
        self,
        epoch,
        iters_per_epoch,
        model,
        data_loader,
        optimizer,
        lr_scheduler,
        scaler=None,
        start_iters=None,
        log_freq=50,
        cuda_enabled=False,
        accum_grad_iters=1,
        rl_start_epoch=0, ##GREEDY FORCING
        global_step=0,
    ) -> Dict[str, float]:
        """
        Inner training loop for one epoch.

        Args:
            epoch (int): The current epoch number.
            iters_per_epoch (int): Number of iterations per epoch.
            model (Any): The model to train.
            data_loader (Any): The data loader for training data.
            optimizer (torch.optim.Optimizer): The optimizer.
            lr_scheduler (Any): The learning rate scheduler.
            scaler (Any, optional): Gradient scaler for mixed precision training.
            start_iters (int, optional): Starting iteration number.
            log_freq (int): Logging frequency. Defaults to 50.
            cuda_enabled (bool): Whether to use CUDA. Defaults to False.
            accum_grad_iters (int): Number of iterations to accumulate gradients. Defaults to 1.

        Returns:
            Dict[str, float]: Dictionary of averaged training stats.
        """
        use_amp = scaler is not None
        metric_logger = MetricLogger(delimiter="  ")
        metric_logger.add_meter("lr", SmoothedValue(window_size=1, fmt="{value:.6f}"))
        metric_logger.add_meter("loss", SmoothedValue(window_size=1, fmt="{value:.4f}"))

        logging.info(
            f"Start training epoch {epoch}, {iters_per_epoch} iters per inner epoch."
        )
        header = f"Train: data epoch: [{epoch}]"
        if start_iters is None:
            inner_epoch = epoch
        else:
            inner_epoch = start_iters // iters_per_epoch
            header = header + f"; inner epoch [{inner_epoch}]"

        if not hasattr(data_loader, "__next__"):
            data_loader = iter(data_loader)

        pbar = tqdm(total=iters_per_epoch, desc=header, leave=False)

        for i in metric_logger.log_every(range(iters_per_epoch), log_freq, header):
            if i >= iters_per_epoch:
                break

            samples = next(data_loader)
            samples = prepare_sample(samples, cuda_enabled=cuda_enabled)
            samples.update(
                {
                    "epoch": inner_epoch,
                    "num_iters_per_epoch": iters_per_epoch,
                    "iters": i,
                }
            )

            lr_scheduler.step(cur_epoch=inner_epoch, cur_step=i)

            with torch.cuda.amp.autocast(enabled=use_amp):
                if epoch < rl_start_epoch:
                    # Initial training phase
                    loss, loss_dict = self.train_step(model=model, samples=samples)
                    loss /= accum_grad_iters
                else:
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = param_group['lr'] * 1e-3 ##GREEDY FORCING
                        
                    loss, loss_dict = self.train_rl_step(model=model, samples=samples)
                    loss /= accum_grad_iters
            
            if use_amp:
                scaler.scale(loss).backward()
            else:
                loss.backward()

            if (i + 1) % accum_grad_iters == 0:
                if use_amp:
                    scaler.step(optimizer)
                    scaler.update()                   
                else:    
                    optimizer.step()
                optimizer.zero_grad()

            metric_logger.update(**loss_dict)
            metric_logger.update(lr=optimizer.param_groups[0]["lr"])

            # Optimize reward weights periodically
            """if epoch >= 2:
                self.evolutionary_algorithm.evolve(task=self, model=model, data_loader=data_loader)"""

            global_step += 1
            pbar.update(1)

        pbar.close()

        # gather the stats from all processes
        metric_logger.synchronize_between_processes()
        logging.info(f"Averaged stats: {metric_logger.global_avg()}")
        return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

    @staticmethod
    def save_result(result: List[Dict[str, Any]], result_dir: str, filename: str, remove_duplicate: str = "") -> str:
        """
        Save the evaluation result to a file.

        Args:
            result (List[Dict[str, Any]]): List of results to save.
            result_dir (str): Directory to save the result file.
            filename (str): Name of the result file.
            remove_duplicate (str, optional): Key to use for removing duplicates. Defaults to "".

        Returns:
            str: Path to the saved result file.
        """
        import json

        result_file = os.path.join(
            result_dir, "%s_rank%d.json" % (filename, get_rank())
        )
        final_result_file = os.path.join(result_dir, "%s.json" % filename)

        json.dump(result, open(result_file, "w"))

        if is_dist_avail_and_initialized():
            dist.barrier()

        if is_main_process():
            logging.warning("rank %d starts merging results." % get_rank())
            # combine results from all processes
            result = []

            for rank in range(get_world_size()):
                result_file = os.path.join(
                    result_dir, "%s_rank%d.json" % (filename, rank)
                )
                res = json.load(open(result_file, "r"))
                result += res

            if remove_duplicate:
                result_new = []
                id_list = []
                for res in result:
                    if res[remove_duplicate] not in id_list:
                        id_list.append(res[remove_duplicate])
                        result_new.append(res)
                result = result_new

            json.dump(result, open(final_result_file, "w"))
            logging.info("result file saved to %s" % final_result_file)

        return final_result_file

def main(cfg: Any):
    """
    Main function to set up and run the reward impact measuring experiments.

    Args:
        cfg (Any): Configuration object.
    """
    # Initialize the task, model, and datasets
    print("Am I being called?"*40)
    task = BaseTask.setup_task()
    model = task.build_model(cfg)
    datasets = task.build_datasets(cfg)
    
    # Setup optimizer and learning rate scheduler
    optimizer = torch.optim.AdamW(model.parameters(), lr=cfg.run_cfg.init_lr)
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=cfg.run_cfg.max_epoch)
    
    # Setup gradient scaler for mixed precision training
    scaler = torch.cuda.amp.GradScaler()

    # Retrieve all reward functions from the registry
    reward_component_names = list(RewardRegistry.registry.keys())

    # Perform Ablation Study
    """logging.info("Starting Ablation Study...")
    task.ablation_study(
        model=model,
        data_loader=datasets['train'],
        reward_component_names=reward_component_names,
        optimizer=optimizer,
        lr_scheduler=lr_scheduler,
        scaler=scaler,
        cfg=cfg,
        epoch_count=1  # Adjust as needed
    )
    logging.info("Ablation Study Complete.")"""

    # Perform Single Reward Experiments
    logging.info("Starting Single Reward Experiments...")
    print("Starting Single Reward Experiments...")
    print("Starting Single Reward Experiments...")
    print("Starting Single Reward Experiments...")
    task.single_reward_experiments(
        model=model,
        data_loader=datasets['train'],
        reward_component_names=reward_component_names,
        optimizer=optimizer,
        lr_scheduler=lr_scheduler,
        scaler=scaler,
        cfg=cfg,
        epoch_count=1  # Adjust as needed
    )
    logging.info("Single Reward Experiments Complete.")

    logging.info("Reward impact experiments complete.")


if __name__ == "__main__":
    config = load_config()  # Load configuration (implement this function)
    main(config)