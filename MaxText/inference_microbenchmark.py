"""
 Copyright 2024 Google LLC

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      https://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
 """

"""Inference microbenchmark for prefill and autoregressive steps."""
import datetime
import jax
import json
import sys

import numpy as np

from jetstream.engine import token_utils

import max_utils
import maxengine
import maxtext_utils
import pyconfig
import inference_utils

def prefill_benchmark_loop(config, engine, decode_state, params, tokens, true_length, iters, profile_name=""):
  """ Inner loop for benchmarking prefill step. """
  all_prefill_seconds = []
  max_utils.activate_profiler(config, profile_name)
  for i in range(iters):
    slot = int(i % (jax.device_count() * config.per_device_batch_size))
    start = datetime.datetime.now()
    prefill_result = engine.prefill(params=params, padded_tokens=tokens, true_length=true_length)
    jax.block_until_ready(prefill_result)
    end = datetime.datetime.now()
    prefill_seconds = (end - start).total_seconds()
    all_prefill_seconds.append(prefill_seconds)
    decode_state = engine.insert(prefill_result, decode_state, slot=slot)
    jax.block_until_ready(decode_state)
    inference_utils.delete_pytree(prefill_result)
  max_utils.deactivate_profiler(config)
  return np.average(all_prefill_seconds), decode_state


def prefill_benchmark(config, engine, params, decode_state, tokens, true_length,
                      iters=100, profile_name="", num_model_params=None):
  """ Handles init, warmup, running prefill benchmark, and printing results. """
  if num_model_params is None:
    num_model_params, _, _ = inference_utils.summarize_pytree_data(params, name="Params")

  for i in range(2):
    prefill_result = engine.prefill(params=params, padded_tokens=tokens, true_length=true_length)
    decode_state = engine.insert(prefill_result, decode_state, slot=i)
    jax.block_until_ready(decode_state)
    inference_utils.delete_pytree(prefill_result)

  print(f"Prefill results for length {tokens.size}:\n")

  profile_name = f"prefill_{tokens.size}" if profile_name == "" else profile_name
  time_in_s, decode_state = prefill_benchmark_loop(config, engine, decode_state, params, tokens, true_length, iters,
                                                   profile_name=profile_name)
  prefill_average_ms = time_in_s * 1000
  total_prefill_tflops, _, _ = maxtext_utils.calculate_tflops_prefill(num_model_params, tokens.size, config)
  tflops_per_sec_per_device = total_prefill_tflops / jax.device_count() / prefill_average_ms * 1000.
  print(f"\tPrefill step average time: {prefill_average_ms:.3f}ms\n"
        f"\tPrefill total TFLOPs: {total_prefill_tflops:.3f}\n"
        f"\tPrefill TFLOPs/sec/device: {tflops_per_sec_per_device:.3f}\n\n\n\n")
  result_dict = {"prefill_time_in_ms": prefill_average_ms,
                 "prefill_total_tflops": total_prefill_tflops,
                 "prefill_tflops_per_sec_per_device": tflops_per_sec_per_device}
  return result_dict, decode_state


def ar_benchmark_loop(config, engine, decode_state, params, iters, profile_name=""):
  """ Inner loop for benchmarking ar step. """
  all_generate_seconds = []
  max_utils.activate_profiler(config, profile_name)
  for _ in range(iters):
    start = datetime.datetime.now()
    decode_state, _ = engine.generate(params, decode_state)
    jax.block_until_ready(decode_state)
    end = datetime.datetime.now()
    generate_seconds = (end - start).total_seconds()
    all_generate_seconds.append(generate_seconds)

  max_utils.deactivate_profiler(config)
  return np.average(all_generate_seconds), decode_state


def ar_benchmark(config, engine, params, decode_state, cache_size=None, model_size=None, profile_name="", iters=100):
  """ Handles init, warmup, running ar benchmark, and printing results. """
  if cache_size is None:
    _, cache_size, _ = inference_utils.summarize_pytree_data(decode_state['cache'], name="Cache")
  if model_size is None:
    _, model_size, _ = inference_utils.summarize_pytree_data(params, name="Params")
  global_batch_size = jax.device_count() * config.per_device_batch_size

  # Warmup
  for _ in range(2):
    decode_state, _ = engine.generate(params, decode_state)
    jax.block_until_ready(decode_state)

  profile_name = "autoregress" if profile_name == "" else profile_name
  seconds_per_step, decode_state = ar_benchmark_loop(
    config,
    engine,
    decode_state,
    params,
    profile_name=profile_name,
    iters=iters
  )
  ar_average_ms = seconds_per_step*1000
  total_throughput = jax.device_count() * config.per_device_batch_size / seconds_per_step

  GB_per_step_per_device = (model_size + cache_size) / 1e9 / jax.device_count()
  bw_per_device = GB_per_step_per_device/seconds_per_step
  print(f"AutoRegressive results:\n"
        f"\tAR step average time: {ar_average_ms:.3f}ms\n"
        f"\tAR step average time per seq: {ar_average_ms/global_batch_size:.3f}ms\n"
        f"\tAR global batch size: {global_batch_size}\n"
        f"\tAR throughput: {total_throughput:.3f} tokens/second\n"
        f"\tAR memory bandwidth per device: {bw_per_device:.3f} GB/s\n\n\n")


  result_dict = {"ar_step_in_ms": ar_average_ms,
                 "ar_step_in_ms_per_seq": ar_average_ms / global_batch_size,
                 "ar_global_batch_size": global_batch_size,
                 "ar_total_throughput_tokens_per_second": total_throughput,
                 "ar_device_bandwidth_GB_per_second": bw_per_device}
  return result_dict, decode_state


def collate_results(config, results, model_size, cache_size, num_model_params, incl_config=False):
  """ Adds model/cache size info and optionally config info to results. """
  results["sizes"] = {
    "Model_size_in_GB": model_size / 1e9,
    "cache_size_in_GB": cache_size / 1e9,
    "model_params_in_billions": num_model_params / 1e9,
  }
  if incl_config:
    results["config"] = {}
    for k, v in dict(config.get_keys()).items():
      results["config"][k] = str(v) if k == "dtype" else v # json fails with original dtype
  return results


def write_results(results, filename=""):
  if filename != "":
    with open(filename, "w", encoding="utf-8") as f:
      json.dump(results, f, indent=2)


def print_results_for_analyze(results):
  prefill_bucket_size_to_ms = {}
  for k, v in results["Prefill"].items():
    prefill_bucket_size_to_ms[int(k)] = round(v["prefill_time_in_ms"], 3)
  print("\nFor usage in analyze_sharegpt.py :")
  print(f"PREFILL_BUCKET_SIZE_TO_MS = {prefill_bucket_size_to_ms}")
  print(f"SYSTEM_TIME_PER_DECODE_TOKEN_MS = {results['AutoRegressive']['ar_step_in_ms_per_seq']}")


def main(config):
  engine = maxengine.MaxEngine(config)
  params = engine.load_params()
  prefill_lengths = [int(l) for l in config.inference_microbenchmark_prefill_lengths.split(",")]
  stages_to_benchmark = config.inference_microbenchmark_stages.split(",")
  benchmark_loop_iters = 10
  text = config.prompt
  metadata = engine.get_tokenizer()
  vocab = token_utils.load_vocab(metadata.path, metadata.extra_ids)

  decode_state = engine.init_decode_state()
  _, cache_size, _ = inference_utils.summarize_pytree_data(decode_state['cache'], name="Cache")
  num_model_params, model_size, _ = inference_utils.summarize_pytree_data(params, name="Model")

  benchmark_results = {}
  if "prefill" in stages_to_benchmark:
    benchmark_results["Prefill"] = {}
    for prefill_length in prefill_lengths:
      tokens, true_length = token_utils.tokenize_and_pad(
        text, vocab, is_bos=True, prefill_lengths=[prefill_length])
      benchmark_results["Prefill"][prefill_length], decode_state = prefill_benchmark(
        config, engine, params, decode_state, tokens, true_length,
        iters=benchmark_loop_iters, num_model_params=num_model_params)

  if "generate" in stages_to_benchmark:
    benchmark_results["AutoRegressive"], decode_state = ar_benchmark(
      config, engine, params, decode_state, iters=benchmark_loop_iters, cache_size=cache_size, model_size=model_size)

  results = collate_results(config, benchmark_results, model_size, cache_size, num_model_params)
  write_results(results, filename="")
  print_results_for_analyze(results)


if __name__ == "__main__":
  pyconfig.initialize(sys.argv)
  main(pyconfig.config)
