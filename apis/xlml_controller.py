# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The controller to run XL ML tests."""

from airflow.utils import task_group
from apis import gcp_config, task, test_config


def run(
    task_id_suffix: str,
    job_task: task.BaseTask,
    job_gcp_config: gcp_config.GCPConfig,
    job_test_config: test_config.TestConfig,
) -> task_group.TaskGroup:
  """Run a test job.

  Args:
    task_id_suffix: An ID suffix of an Airflow task.
    job_task: Tasks for a test job.
    job_gcp_config: Configs of GCP to run a test job.
    job_test_config: Configs for a test job.

  Returns:
    A task group with chained up four tasks: provision, run_model, post_process
    and clean_up.
  """
  with task_group.TaskGroup(group_id=f"run_{task_id_suffix}") as tg:
    provision = job_task.provision(
        task_id_suffix, job_gcp_config, job_test_config
    )
    run_model = job_task.run_model(
        task_id_suffix, job_gcp_config, job_test_config
    )
    post_process = job_task.post_process(
        task_id_suffix, job_gcp_config, job_test_config
    )
    clean_up = job_task.clean_up(task_id_suffix, job_gcp_config)

    provision >> run_model >> post_process >> clean_up

  return tg