"""Fine-tune Helsinki-NLP/opus-mt-tc-big-en-zle on our EN->UK pairs (README.md). Low learning rate and early
stopping on the dev loss, so the model learns our domain (games, spoken ASR lines, N0/N1) without forgetting
general language.  python train.py <prep dir> <out dir>   (GPU; env LR, EPOCHS, BATCH, BASE_REVISION)"""
import os, sys

import torch
from datasets import Dataset
from transformers import (DataCollatorForSeq2Seq, EarlyStoppingCallback, MarianMTModel, MarianTokenizer,
                          Seq2SeqTrainer, Seq2SeqTrainingArguments)

P, OUT = sys.argv[1], sys.argv[2]
BASE = "Helsinki-NLP/opus-mt-tc-big-en-zle"
REV = os.environ.get("BASE_REVISION", "main")
LR = float(os.environ.get("LR", "2e-5"))
EPOCHS = float(os.environ.get("EPOCHS", "6"))
BATCH = int(os.environ.get("BATCH", "16"))


def load(name):
    rows = [l.rstrip("\n").split("\t") for l in open(os.path.join(P, name), encoding="utf-8")]
    return Dataset.from_dict({"en": [">>ukr<< " + r[0] for r in rows], "uk": [r[1] for r in rows]})


tok = MarianTokenizer.from_pretrained(BASE, revision=REV)
model = MarianMTModel.from_pretrained(BASE, revision=REV)


def enc(b):
    x = tok(b["en"], max_length=256, truncation=True)
    x["labels"] = tok(text_target=b["uk"], max_length=256, truncation=True)["input_ids"]
    return x


train = load("train_aug.tsv").map(enc, batched=True, remove_columns=["en", "uk"])
dev = load("dev.tsv").map(enc, batched=True, remove_columns=["en", "uk"])
args = Seq2SeqTrainingArguments(
    output_dir=OUT, learning_rate=LR, num_train_epochs=EPOCHS, per_device_train_batch_size=BATCH,
    per_device_eval_batch_size=BATCH * 2, weight_decay=0.01, warmup_ratio=0.05, lr_scheduler_type="linear",
    eval_strategy="epoch", save_strategy="epoch", save_total_limit=2, load_best_model_at_end=True,
    metric_for_best_model="eval_loss", greater_is_better=False, fp16=torch.cuda.is_available(),
    logging_steps=50, report_to=[], seed=13, label_smoothing_factor=0.1)
trainer = Seq2SeqTrainer(model=model, args=args, train_dataset=train, eval_dataset=dev, processing_class=tok,
                         data_collator=DataCollatorForSeq2Seq(tok, model=model),
                         callbacks=[EarlyStoppingCallback(early_stopping_patience=2)])
trainer.train()
best = os.path.join(OUT, "best")
trainer.save_model(best)
tok.save_pretrained(best)
print("best checkpoint:", trainer.state.best_model_checkpoint, "eval_loss", trainer.state.best_metric)
