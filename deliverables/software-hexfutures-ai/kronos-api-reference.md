# Kronos 集成参考（精确 API，供工程师实现用）

来源：github.com/shiyu-coder/Kronos（README.md + requirements.txt，2026-04 实测）

## 1. 依赖（requirements.txt 原文）
```
numpy
pandas
torch>=2.0.0
einops==0.8.1
huggingface_hub==0.33.1
matplotlib==3.9.3
pandas==2.2.2
tqdm==4.67.1
safetensors==0.6.2
```
> 注：`finetune/qlib_test.py` 回测还需 `pyqlib`（`pip install pyqlib`）；Tokenizer/Predictor 实际可能还需 `transformers`。务必用虚拟环境隔离安装。

## 2. 预测 API（零样本 / 推理）
```python
from model import Kronos, KronosTokenizer, KronosPredictor

# 1) 加载分词器与模型（从 Hugging Face Hub）
tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
model     = Kronos.from_pretrained("NeoQuasar/Kronos-small")   # mini(4.1M)/small(24.7M)/base(102.3M)

# 2) 实例化预测器（max_context 控制上下文长度，base 默认 512）
predictor = KronosPredictor(model, tokenizer, max_context=512)

# 3) 生成预测
pred_df = predictor.predict(
    df=x_df,                 # 输入 K 线 DataFrame（需含 open/high/low/close/volume/amount）
    x_timestamp=x_timestamp, # 输入时间戳序列
    y_timestamp=y_timestamp, # 预测目标时间戳序列
    pred_len=pred_len,       # 预测步长
    T=1.0,                   # 采样温度
    top_p=0.9,               # 核采样概率
    sample_count=1           # 生成并平均的预测路径数
)

# 返回值：pandas DataFrame，列为 open/high/low/close/volume/amount，索引为 y_timestamp
print(pred_df.head())
```

## 3. 微调（Finetune）流程
```shell
# 步骤 1：准备数据集（qlib 格式预处理）
python finetune/qlib_data_preprocess.py

# 步骤 2：微调 Tokenizer（多卡示例）
torchrun --standalone --nproc_per_node=NUM_GPUS finetune/train_tokenizer.py

# 步骤 3：微调 Predictor（多卡示例）
torchrun --standalone --nproc_per_node=NUM_GPUS finetune/train_predictor.py

# 步骤 4：回测评估（指定 GPU）
python finetune/qlib_test.py --device cuda:0
```

## 4. 到中国商品期货的适配要点（自研管线）
- 开源示例仅 A 股/BTC，**中国商品期货需自建数据管线**：用天勤 TQSDK / RQAlpha / SIMNOW 取沪铜、螺纹钢、原油等日线/小时线 OHLCV，转成 Kronos 所需的 `df`（open/high/low/close/volume/amount）+ 时间戳。
- 微调时用 `finetune_csv/` 目录（README 提到 `finetune_csv` 配置，适合自有 CSV 数据）替代 qlib 管线，降低对 pyqlib 的依赖。
- 预测输出为未来 OHLCV，需后处理成"方向/涨跌概率信号"喂给 RL 层与风控层。
- 自回归推理对显存有要求；本地 GPU 建议先用 Kronos-small(24.7M) 或 mini(4.1M) 起步。

## 5. 模型族（HuggingFace）
- Kronos-mini  → NeoQuasar/Kronos-mini  (4.1M)
- Kronos-small → NeoQuasar/Kronos-small (24.7M)
- Kronos-base  → NeoQuasar/Kronos-base  (102.3M)
- Kronos-large → 未开源 (499.2M)
- Tokenizer: Kronos-Tokenizer-2k (mini) / Kronos-Tokenizer-base (small/base)
