#!/bin/sh
# Builds the optional duplicate-check model (rerank.py): Qwen3-Reranker-0.6B (Apache-2.0) as int8 ONNX plus its
# tokenizer, in the folder given as $1. Needs uv and ~6 GB of disk for the download and the fp32 step (deleted after).
# Then copy the folder to the server and point KNOWLEDGE_RERANK_MODEL at it.
set -eu
out=${1:?usage: export_reranker.sh <output folder>}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
uv run --no-project --python 3.12 --with "optimum-onnx[onnxruntime]" --with torch \
  --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
  optimum-cli export onnx --model tomaarsen/Qwen3-Reranker-0.6B-seq-cls --task text-classification "$tmp/fp32"
mkdir -p "$out"
uv run --no-project --with onnxruntime==1.31.0 --with onnx python -c "
from onnxruntime.quantization import quantize_dynamic, QuantType
quantize_dynamic('$tmp/fp32/model.onnx', '$out/model.onnx', weight_type=QuantType.QInt8)"
cp "$tmp/fp32/tokenizer.json" "$out/"
echo "done: $out (model.onnx + tokenizer.json)"
