<h1 align="center">
  <img src="https://i.imgur.com/TBRZwDu.png" alt="Logo" width="800" height="200">
</h1>

<div align="center">
LAVIS's InstructBLIP model finetuned to remote sensing image-text data via Reinforcement Learning. The aim is to teach Visual Reasoning to a VLM on Remote Sensing imagery: Visual Reasoning data is quite scarce in the domain of remote sensing, the goal of this RL finetuning is to better exploit the existing data and to "enforce" Visual Reasoning in RS VLMs.
</div>

<div align="center">
<br />

[![license](https://img.shields.io/github/license/dec0dOS/amazing-github-template.svg?style=flat-square)](LICENSE)

</div>

<details open="open">
<summary>Table of Contents</summary>

- [About](#-about)
  - [Built With](#-built-with)
  - [Diagram of the model](#-diagram-of-the-model)
  - [Qualitative results](#-qualitative-results)
  - [Quantitative results](#-quantitative-results)
- [Getting Started](#-getting-started)
  - [Prerequisites](#prerequisites)
- [Training configuration](#%EF%B8%8F-training-configuration)
- [Start training](#-start-training)
- [Learning signals registry](#%EF%B8%8F-learning-signals-registry)
- [Best Model](#-best-model)
- [License](#-license)
- [Acknowledgements](#-acknowledgements)

</details>

---

## ❓ About

<table>
<tr>
<td>

Forked from SalesForce's LAVIS repository, this improved version implements Reinforcement Learning to bolster image captioning abilities for the specific domain of remote sensing. On top of optimization through Cross-Entropy loss minimization, a few supplementary Reinforcement Learning epochs are completed to guide the model towards more desirable outputs, using learning signals tailored to the domain of Remote Sensing. More precisely, **Self-Critical Sequence Training** (<a>https://arxiv.org/abs/1612.00563), a variant of the **REINFORCE** algorithm which is similar to PPO, is used to enforce these learning signals.  

<details open>
<summary>Additional info</summary>
<br>
Note that SCST can be made compatible with PPO/GRPO, with the issue that there are no intermediate rewards during the generation of a caption (the full generated caption is required to compute the learning signals).
</details>

</td>
</tr>
</table>

## 🛠 Built With
- 🏗 **[SalesForce's LAVIS](https://github.com/salesforce/LAVIS)** - Core vision-language model, easily adaptable to RL
- 📊 **[FACTUAL Scene Graph Extractor](https://github.com/zhuang-li/FactualSceneGraph)** - One of the most impactful reward function is obtained by measuring the closeness of generated captions and ground-truth (human-annotated) captions. FACTUAL extracts "scene graphs", like the SPICE metric, to compute such a reward by comparing the graphs. It also highlights the missing objects and the hallucinations made by the model.

## 📈 Diagram of the model
<h1 align="center">
  <img src="https://i.imgur.com/AasnyVG.png" alt="Figure BLIP_SCST" width="850" height="220">
</h1>

## 📊 Qualitative results



## 📈 Quantitative results

Experiments were conducted on <a href="https://github.com/201528014227051/RSICD_optimal">RSICD</a>, <a href="https://mega.nz/folder/wCpSzSoS#RXzIlrv--TDt3ENZdKN8JA">UCM-Captions</a>, and on <a href="https://huggingface.co/datasets/xiang709/VRSBench">VRSBench</a>.

When evaluated on RSICD using standard captioning metrics, our method demonstrates SOTA performances. CE = Cross-Entropy loss training.

<h1 align="center">
  <img src="https://i.imgur.com/2McI0hm.png" alt="RSICD_standard_metrics" width="600" height="150" class="center">
</h1>

### 📈 Custom metrics (oversights, hallucinations)

**RSICD Dataset**

<h1 align="center">
  <img src="https://i.imgur.com/QZIOPNb.png" alt="RSICD oversights/hallucinations" width="700" height="150" class="center">
</h1>

The up and down arrows next to the scores indicate the direction towards which the score should go to improve the score. For instance, **-2,62%** in the "oversights" column on the first line is in accordance with the arrow's direction, meaning that it is going in the right direction.

**UCM Dataset**

<h1 align="center">
  <img src="https://i.imgur.com/mJ07kjS.png" alt='UCM" width="700" height="150" class='center'>

### Reward functions (A.K.A. learning signals)

**NKL**:  **N**egative **K**ullback-**L**eibler **D**ivergence. Using a small language model (a pretrained BERT model from spaCy), we compute embeddings for every tokens of the ground-truth captions and of the generated captions. This yields two distributions of embeddings, that we try to bring closer by minimizing their KL-Divergence.

**CIDEr**: a classic captioning metric that relies on TF-IDF vectors ressemblance between two sentences to compare.  

**length**: opposite of the number of tokens in the generated caption (since the policy loss is being minimized, we must minimize its opposite to maximize the length of generated captions).  

**SDE**: **S**cene **D**escription **E**xhaustiveness, **proportion of entities in the generated caption present in the ground-truth caption(s)**, and serves the purpose of **getting ground-truth captions entities into generated captions**, to align with the expert human annotators. Entities are lemmatized before this score is computed, to avoid false negatives.  

SDE computation example: 
```sh
• **Generated caption**: There is a _forest_. (object: forest)
• **Ground-truth caption 1**: There is a _forest_ and a _river_. (objects: forest, river)
• **Ground-truth caption 2**: There is a _forest_, a _river_ and a _road_. (objects: forest, river, road)

Objects detected in the human-annotated (ground-truth) captions: **forest, river, road** (3 objects)
Object detected in the model's output caption: **forest** (1 object)

Therefore, the SDE score is **1/3** in this example.
```
#### ➕ Addendum to the policy loss of SCST

Another loss term, termed **V/E** for **Varentropy/Entropy**, is **jointly minimized with the policy loss**. Inspired by <a href="https://github.com/xjdr-alt/entropix">Entropix</a>, the point is to balance between **diverse vocabulary usage (high entropy)** and **consistent token distributions (low varentropy)**. This significantly limits degenerate generated tokens distributions, and encourages vocabulary exploration at the same time, which increases the model's vocabulary by taking inspiration from the human-annotated captions.
The ```math 
\lambda = 10^{-4}
```
Weighting is the weight we multiply the **V/E** term with to control its magnitude.

## 🚀 Getting Started

### Prerequisites

- Clone the present repository (installing the original LAVIS repository will require multiple precise modifications in the code that have already been done in this very repository).

#### RS-LAVIS with RL

- After installing this repository, you need to create an environment, activate it, and install the libraries from requirements.txt. **PYTHON 3.9+ REQUIRED**

#### conda
```sh
conda create --name lavis_rl python=3.9
conda activate lavis_rl
```

#### pip
```sh
pip install -r requirements.txt
```

#### FACTUAL Scene Graph Extraction
Crucial for the "Object Proportion" (SDE) learning signal to work.
```sh
pip install FactualSceneGraph
```
OR choose a pretrained model from huggingface: <a>https://github.com/zhuang-li/FactualSceneGraph</a>

## 🎛️ Training configuration

The training configuration for captioning can be found here: <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/blob/main/lavis/projects/blip2/train/caption_rs_ft.yaml">lavis/projects/blip2/train/caption_rs_ft.yaml</a>

#### BLIP2 models

Alternative frozen vision encoders can be used with BLIP2. They can be found in <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/tree/main/lavis/configs/models/blip2">lavis/configs/models/blip2</a>.

#### Datasets configurations

The .yaml file for dataset configuration may be found here: <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/blob/main/lavis/configs/datasets/rs/defaults_cap.yaml">lavis/configs/datasets/rs/defaults_cap.yaml</a>. The image folder must contain every image from the dataset, regardless of the split they belong to. The JSON files containing the captions for the train, val and test splits must be in <a href="https://auto.gluon.ai/dev/tutorials/multimodal/object_detection/data_preparation/convert_data_to_coco_format.html">COCO format</a>.

Object detector based "pseudo-captioning" can be activated by editing lines 48 and 72 from <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/blob/main/lavis/datasets/datasets/rs.py">lavis/datasets/datasets/rs.py</a>. This can slightly improve performances.

In case you need to modify the dataset config, edit this code: <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/blob/main/lavis/datasets/builders/rs_caption.py">lavis/datasets/builders/rs_caption.py</a>.

Finally, set the paths to your val and test json files in <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/blob/main/lavis/tasks/captioning.py">lavis/tasks/captioning.py, lines 138-139</a>

## ⌛ Start training

Once everything is correctly installed and configured, run the following command:

```sh
python train.py --cfg-path your_main_folder/LAVIS/lavis/projects/blip2/train/caption_rs_ft.yaml --model_name eva_clip_g_plus
```

## 🏆 Best model

Weights for the best InstructBLIP model we have obtained.
<a>https://huggingface.co/tdujardin/InstructBLIP_RS_RL/tree/main</a>

## ⚙️ Learning signals registry

The "rewards.py" registry of learning signals may be found in <a href="https://github.com/zhu-xlab/InstructBLIP_SCST/blob/main/lavis/tasks/rewards.py> "InstructBLIP_SCST/lavis/tasks/rewards.py</a>

## 🧾 License

This project is licensed under the **MIT license**.

## 🙏 Acknowledgements

We extend our gratitude to **SalesForce** for developing the **LAVIS** repository, which provides an intuitive Vision-Language models library. Implementing Reinforcement Learning was made significantly easier by their work.

Additionally, one of our main learning signals for RL was based on <a href="https://github.com/zhuang-li/FactualSceneGraph">**FACTUAL**</a>, a finetuned FLAN-T5 model that extracts scene graphs.
