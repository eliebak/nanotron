""" Mostly complete a SLURM template with a link to a single checkpoint on s3 and launch it
"""
import datetime
import os
import re
import subprocess
from typing import List, Optional, Tuple, Union
import copy

import jinja2
from nanotron import logging
from nanotron.logging import log_rank
from nanotron.parallel import ParallelContext

from nanotron.config import Config, LightEvalConfig

logger = logging.get_logger(__name__)


class LightEvalRunner:
    def __init__(self, config: Config, parallel_context: Optional[ParallelContext] = None):
        self.config = config
        self.lighteval_config = config.lighteval
        self.parallel_context = parallel_context

    def eval_single_checkpoint_no_s3(self, checkpoints_folder, current_step) -> Tuple[str, str]:
        current_checkpoint_folder = os.path.join(checkpoints_folder, str(current_step))
        checkpoint_path = os.path.join(current_checkpoint_folder, "config.yaml")
        if not os.path.exists(checkpoint_path):
            log_rank(
                f"Checkpoint path does not exist: {checkpoint_path}. Unable to evaluate.",
                logger=logger,
                level=logging.ERROR,
                group=self.parallel_context.dp_pg if self.parallel_context is not None else None,
                rank=0,
            )
            return None, None

        slurm_job_id, slurm_log = run_slurm_one_job(
            config = self.config,
            slurm_template=self.lighteval_config.slurm_template,
            model_checkpoint_path=checkpoint_path,
            current_step=self.config.general.step,
            checkpoint_local_path=current_checkpoint_folder,
            s3=False,
        )

        return slurm_job_id, slurm_log

    def eval_single_checkpoint(self, uploaded_files: List[dict]) -> Tuple[str, str]:
        """Run light evaluation on uploaded files."""
        logger.warning(f"Lighteval Runner got {len(uploaded_files)} files. Checking configs.")
        config_files = [
            f for f in uploaded_files if "config.py" in f["destination"] or "config.yaml" in f["destination"]
        ]
        # Sanity check on the config files len (we want only one)
        if len(config_files) == 0:
            log_rank(
                "No config files founds in uploaded checkpoints. Not running evaluation.",
                logger=logger,
                level=logging.ERROR,
                group=self.parallel_context.dp_pg if self.parallel_context is not None else None,
                rank=0,
            )
            return
        if len(config_files) > 1:
            log_rank(
                "Found multiple config files in uploaded checkpoints.",
                logger=logger,
                level=logging.ERROR,
                group=self.parallel_context.dp_pg if self.parallel_context is not None else None,
                rank=0,
            )
            return
        checkpoint_path = config_files[0]["destination"].replace("config.yaml", "")

        slurm_job_id, slurm_log = run_slurm_one_job(
            config = self.config,
            lighteval_config = self.lighteval_config,
            slurm_template=self.lighteval_config.slurm_template,
            model_checkpoint_path=checkpoint_path,
            current_step=self.config.general.step,
            s3=True,
        )

        return slurm_job_id, slurm_log


def run_slurm_one_job(
    config: Config,
    lighteval_config: LightEvalConfig,
    model_checkpoint_path: str,
    slurm_template: str,
    current_step: int,
    s3: bool = True,
    checkpoint_local_path: str = None,
    slurm_name: Optional[str] = "eval",
):
    """Launch a single job on Slurm with the given mapping
    Args:
        slurm_config: Slurm configuration
        mapping: Mapping to use for the job script (see SLURM_ONE_JOB_MAPPING)
    """

    eval_launch_script_path = os.path.join(config.general.evals_logs_path, "launch-config", str(current_step))
    eval_logs_path = os.path.join(config.general.evals_logs_path, "logs", str(current_step))

    os.makedirs(eval_launch_script_path, exist_ok=True)
    os.makedirs(eval_logs_path, exist_ok=True)

    environment = jinja2.Environment(
        comment_start_string="{=",
        comment_end_string="=}",
    )

    with open(slurm_template, "r") as f:
        SLURM_JOBS_ARRAY_TEMPLATE = environment.from_string(f.read())

    #Not sure if this is the best way to do it. Maybe we need to add a copy or deepcopy somewhere.
    eval_slurm_config = config.general.eval_slurm_config

    # Update the config with additional required parameters
    # Calculate the number of nodes based on parallelism config and gpus_per_node
    total_gpus_needed = lighteval_config.parallelism.dp * lighteval_config.parallelism.pp * lighteval_config.parallelism.tp
    gpus_per_node = eval_slurm_config.get('gpus_per_node')
    nodes = (total_gpus_needed + gpus_per_node - 1) // gpus_per_node  # Ceiling division
    
    eval_slurm_config.update({
        'nodes': nodes,  # Assuming we want to run on a single node
        'job_name': f"eval-{current_step}",
        'eval_path': eval_logs_path,
        'local_path': config.lighteval.temp_dir if s3 else checkpoint_local_path,
        'hf_user_or_org': config.logging.hf_user_or_org if hasattr(config.logging, 'hf_user_or_org') else None,
        "model_checkpoint_path": model_checkpoint_path,
    })


    launch_string = SLURM_JOBS_ARRAY_TEMPLATE.render(**eval_slurm_config)

    match = re.match(r"#SBATCH --output=(.*)", launch_string)
    slurm_output_path = match.group(1) if match else ""

    current_time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    launch_script_path = os.path.join(eval_launch_script_path, f"launch_script-{current_time}.slurm")

    # make sure the folder exists before write
    # Extract the folder path from launch_script_path
    folder_path = os.path.dirname(launch_script_path)

    # Check if the folder exists. If not, create it.
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)

    with open(launch_script_path, "w") as f:
        f.write(launch_string)

    logger.warning(f'Launching Slurm job {slurm_name} with launch script "{launch_script_path}"')

    # Preserve important environment variables
    env = {
        'PATH': os.environ['PATH'],
        'LD_LIBRARY_PATH': os.environ.get('LD_LIBRARY_PATH', ''),
        'HOME': os.path.expanduser("~"),
    }

    try:
        # Use subprocess.run instead of check_output for better error handling
        result = subprocess.run(
            ["sbatch", launch_script_path],
            env=env,
            check=True,
            capture_output=True,
            text=True
        )
        output = result.stdout
        job_ids = output.split()[-1]
        output_log = (
            slurm_output_path.replace("%x", slurm_name).replace("%j", job_ids).replace("%n", "0").replace("%t", "0")
        )
        logger.warning(f'Slurm job launched successfully with id={job_ids}, logging outputs at "{output_log}"')
    except subprocess.CalledProcessError as e:
        logger.error(f"Error while launching Slurm job: {e}")
        logger.error(f"Command output: {e.output}")
        logger.error(f"Command stderr: {e.stderr}")
        job_ids = None
        output_log = None

    return job_ids, output_log
