# (C) Copyright 2024 European Centre for Medium-Range Weather Forecasts.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

import io
import os
import re
import sys
import time
from argparse import Namespace
from pathlib import Path
from threading import Thread
from typing import Any
from typing import Literal
from typing import Optional
from typing import Union
from weakref import WeakValueDictionary

import requests
from pytorch_lightning.loggers.mlflow import MLFlowLogger
from pytorch_lightning.loggers.mlflow import _convert_params
from pytorch_lightning.loggers.mlflow import _flatten_dict
from pytorch_lightning.utilities.rank_zero import rank_zero_only

from anemoi.training.diagnostics.mlflow.auth import TokenAuth
from anemoi.training.utils.logger import get_code_logger

LOGGER = get_code_logger(__name__)


def health_check(tracking_uri):
    """Query the health endpoint of an MLflow server.
    If the server is not reachable, raise an error and remind the user that authentication may be required."""

    token = os.getenv("MLFLOW_TRACKING_TOKEN")

    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(f"{tracking_uri}/health", headers=headers, timeout=60)

    if response.text == "OK":
        return

    error_msg = f"Could not connect to MLflow server at {tracking_uri}. "
    if not token:
        error_msg += "The server may require authentication, did you forget to turn it on in the config?"
    raise ConnectionError(error_msg)


def get_mlflow_run_params(config, tracking_uri):
    run_id = None
    tags = {"projectName": config.diagnostics.log.mlflow.project_name}
    # create a tag with the command used to run the script
    tags["command"] = sys.argv[0].split("/")[-1]  # get the python script name
    if len(sys.argv) > 1:
        # add the arguments to the command tag
        tags["command"] = tags["command"] + " " + " ".join(sys.argv[1:])
    if config.training.run_id or config.training.fork_run_id:
        "Either run_id or fork_run_id must be provided to resume a run."

        import mlflow

        if config.diagnostics.log.mlflow.authentication and not config.diagnostics.log.mlflow.offline:
            TokenAuth(tracking_uri).authenticate()

        mlflow_client = mlflow.MlflowClient(tracking_uri)

        if config.training.run_id and config.diagnostics.log.mlflow.on_resume_create_child:
            parent_run_id = config.training.run_id  # parent_run_id
            run_name = mlflow_client.get_run(parent_run_id).info.run_name
            tags["mlflow.parentRunId"] = parent_run_id
            tags["resumedRun"] = "True"  # tags can't take boolean values
        elif config.training.run_id and not config.diagnostics.log.mlflow.on_resume_create_child:
            run_id = config.training.run_id
            run_name = mlflow_client.get_run(run_id).info.run_name
            mlflow_client.update_run(run_id=run_id, status="RUNNING")
            tags["resumedRun"] = "True"
        else:
            parent_run_id = config.training.fork_run_id
            tags["forkedRun"] = "True"
            tags["forkedRunId"] = parent_run_id

    if config.diagnostics.log.mlflow.run_name:
        run_name = config.diagnostics.log.mlflow.run_name
    else:
        import uuid

        run_name = f"{uuid.uuid4()!s}"
    return run_id, run_name, tags


class LogsMonitor:
    """Class for logging terminal output.

    Inspired by the class for logging terminal output in aim.
    Aim-Code: https://github.com/aimhubio/aim/blob/94646d2d317ec7a43303a16530f7963e4e652921/aim/ext/resource/tracker.py#L20

    Note: If there is an error, the terminal output logging ends before the error message is printed into the log file.
    In order for the user to see the error message, the user must look at the slurm output file.
    We provide the SLRM job id in the very beginning of the log file and print the final status of the run in the end.

    Parameters
    ----------
    artifact_save_dir : str
        Directory to save the terminal logs.
    experiment : MLflow experiment object.
        MLflow experiment object.
    run_id: str
        MLflow run ID.
    log_time_interval : int
        Interval (in seconds) at which to write buffered terminal outputs, default 30
    """

    _buffer_registry = WeakValueDictionary()
    _old_out_write = None
    _old_err_write = None

    def __init__(self, artifact_save_dir, experiment, run_id, log_time_interval=30.0) -> None:
        # active run
        self.experiment = experiment
        self.run_id = run_id

        # terminal log capturing
        self._log_capture_interval = 1
        self._log_time_interval = log_time_interval
        self._old_out = None
        self._old_err = None
        self._io_buffer = io.BytesIO()

        # Start thread to collect stats and logs at intervals
        self._th_collector = Thread(target=self._log_collector, daemon=True)
        self._shutdown = False
        self._started = False

        # open your files here
        self.artifact_save_dir = artifact_save_dir
        self.file_save_path = Path(artifact_save_dir, "terminal_log.txt")
        self.file_save_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _install_stream_patches(cls) -> None:
        cls._old_out_write = sys.stdout.write
        cls._old_err_write = sys.stderr.write

        def new_out_write(data) -> None:
            # out to buffer
            cls._old_out_write(data)
            if isinstance(data, str):
                data = data.encode()
            for buffer in cls._buffer_registry.values():
                buffer.write(data)

        def new_err_write(data) -> None:
            # err to buffer
            cls._old_err_write(data)
            if isinstance(data, str):
                data = data.encode()
            for buffer in cls._buffer_registry.values():
                buffer.write(data)

        sys.stdout.write = new_out_write
        sys.stderr.write = new_err_write

    @classmethod
    def _uninstall_stream_patches(cls) -> None:
        sys.stdout.write = cls._old_out_write
        sys.stderr.write = cls._old_err_write

    def start(self) -> None:
        """Start collection."""
        if self._started:
            return
        self._started = True
        # install the stream patches if not done yet
        if not self._buffer_registry:
            self._install_stream_patches()
        self._buffer_registry[id(self)] = self._io_buffer
        # Start thread to asynchronously collect logs
        self._th_collector.start()
        LOGGER.info("Termial Log Path: " + str(self.file_save_path))
        if os.getenv("SLURM_JOB_ID"):
            LOGGER.info("SLURM job id: " + os.getenv("SLURM_JOB_ID"))

    def finish(self, status) -> None:
        """Stop the monitoring and close the log file."""
        if not self._started:
            return
        LOGGER.info(
            "Stopping terminal log monitoring and saving buffered terminal outputs. Final status: "
            + status.upper()
            + "."
        )
        self._shutdown = True
        # read and store remaining buffered logs
        self._store_buffered_logs()
        # unregister the buffer
        del self._buffer_registry[id(self)]
        # uninstall stream patching if no buffer is left in the registry
        if not self._buffer_registry:
            self._uninstall_stream_patches()

        with self.file_save_path.open("a") as logfile:
            logfile.write("\n\n")
            logfile.flush()
            logfile.close()

    def _log_collector(self) -> None:
        """Log collecting thread body.

        Main monitoring loop, which consistently collect and log outputs.
        """
        log_capture_time_counter = 0

        while True:
            if self._shutdown:
                break

            time.sleep(self._log_time_interval)  # in seconds
            log_capture_time_counter += self._log_time_interval

            if log_capture_time_counter > self._log_capture_interval:
                self._store_buffered_logs()
                log_capture_time_counter = 0

    def _store_buffered_logs(self) -> None:
        _buffer_size = self._io_buffer.tell()
        if not _buffer_size:
            return
        self._io_buffer.seek(0)
        # read and reset the buffer
        data = self._io_buffer.read(_buffer_size)
        self._io_buffer.seek(0)
        # handle the buffered data and store
        # split lines and keep \n at the end of each line
        lines = [e + b"\n" for e in data.split(b"\n") if e]

        ansi_csi_re = re.compile(b"\001?\033\\[((?:\\d|;)*)([a-dA-D])\002?")

        def _handle_csi(line):
            # removes the cursor up and down symbols from the line
            # skip tqdm status bar updates ending with "curser up" but the last one in buffer to save space
            def _remove_csi(line):
                # replacing the leftmost non-overlapping occurrences of pattern ansi_csi_re in string line by the replacement ""
                return re.sub(ansi_csi_re, b"", line)

            for match in ansi_csi_re.finditer(line):
                arg, command = match.groups()
                arg = int(arg.decode()) if arg else 1
                if command == b"A" and (
                    b"0%" not in line and not self._shutdown and b"[INFO]" not in line and b"[DEBUG]" not in line
                ):  # cursor up
                    # only keep x*10% status updates from tqmd status bars that end with a cursor up
                    # always keep shutdown commands
                    # always keep logger info and debug prints
                    line = b""
            return _remove_csi(line)

        line = None
        with self.file_save_path.open("a") as logfile:
            for line in lines:
                # handle cursor up and down symbols
                line = _handle_csi(line)
                # handle each line for carriage returns
                line = line.rsplit(b"\r")[-1]
                logfile.write(line.decode())

            logfile.flush()
        self.experiment.log_artifact(self.run_id, str(self.file_save_path))


class AnemoiMLflowLogger(MLFlowLogger):
    """A custom MLflow logger that logs terminal output."""

    def __init__(
        self,
        experiment_name: str = "lightning_logs",
        run_name: Optional[str] = None,
        tracking_uri: Optional[str] = os.getenv("MLFLOW_TRACKING_URI"),
        tags: Optional[dict[str, Any]] = None,
        save_dir: Optional[str] = "./mlruns",
        log_model: Literal[True, False, "all"] = False,
        prefix: str = "",
        resumed: Optional[bool] = False,
        forked: Optional[bool] = False,
        run_id: Optional[str] = None,
        offline: Optional[bool] = False,
        authentication: Optional[bool] = None,
        log_hyperparams: Optional[bool] = True,
        # artifact_location: Optional[str] = None,
        # avoid passing any artifact location otherwise it would mess up the offline logging of artifacts
    ) -> None:
        if offline:
            # OFFLINE - When we run offline we can pass a save_dir pointing to a local path
            tracking_uri = None

        else:
            # ONLINE - When we pass a tracking_uri to mlflow then it will ignore the
            # saving dir and save all artifacts/metrics to the remote server database
            save_dir = None

        self._resumed = resumed
        self._forked = forked
        self._flag_log_hparams = log_hyperparams

        if rank_zero_only.rank == 0:
            enabled = authentication and not offline
            self.auth = TokenAuth(tracking_uri, enabled=enabled)

            if offline:
                LOGGER.info("MLflow is logging offline.")
            else:
                LOGGER.info(f"MLflow token authentication {'enabled' if enabled else 'disabled'} for {tracking_uri}")
                self.auth.authenticate()
                health_check(tracking_uri)

        super().__init__(
            experiment_name=experiment_name,
            run_name=run_name,
            tracking_uri=tracking_uri,
            tags=tags,
            save_dir=save_dir,
            log_model=log_model,
            prefix=prefix,
            run_id=run_id,
        )

    @property
    def experiment(self):
        if rank_zero_only.rank == 0:
            self.auth.authenticate()
        return super().experiment

    @rank_zero_only
    def log_system_metrics(self) -> None:
        """Log system metrics (CPU, GPU, etc)."""
        import mlflow
        from mlflow.system_metrics.system_metrics_monitor import SystemMetricsMonitor

        mlflow.enable_system_metrics_logging()
        system_monitor = SystemMetricsMonitor(
            self.run_id,
            resume_logging=self.run_id is not None,
        )
        self.run_id_to_system_metrics_monitor = {}
        self.run_id_to_system_metrics_monitor[self.run_id] = system_monitor
        system_monitor.start()

    @rank_zero_only
    def log_terminal_output(self, artifact_save_dir="") -> None:
        """Log terminal logs to MLflow."""
        # path for logging terminal logs
        # for now the 'terminal_logs' file is kept in the same folder as the plots
        artifact_save_dir = Path(artifact_save_dir, self.run_id, "plots")

        log_monitor = LogsMonitor(
            artifact_save_dir,
            self.experiment,
            self.run_id,
        )
        self.run_id_to_log_monitor = {}
        self.run_id_to_log_monitor[self.run_id] = log_monitor
        log_monitor.start()

    def _clean_params(self, params):
        """Clean up params to avoid issues with mlflow.

        Too many logged params will make the server take longer to render the
        experiment.
        """
        prefixes_to_remove = ["hardware", "data", "dataloader", "model", "training", "diagnostics", "metadata.config"]
        keys_to_remove = [key for key in params if any(key.startswith(prefix) for prefix in prefixes_to_remove)]
        for key in keys_to_remove:
            del params[key]
        return params

    @rank_zero_only
    def log_hyperparams(self, params: Union[dict[str, Any], Namespace]) -> None:
        """Overwrite the log_hyperparams method to flatten config params using '.'."""
        if self._flag_log_hparams:
            params = _convert_params(params)
            params = _flatten_dict(params, delimiter=".")  # Flatten dict with '.' to not break API queries
            params = self._clean_params(params)

            from mlflow.entities import Param

            # Truncate parameter values to 250 characters.
            # TODO: MLflow 1.28 allows up to 500 characters: https://github.com/mlflow/mlflow/releases/tag/v1.28.0
            params_list = [Param(key=k, value=str(v)[:250]) for k, v in params.items()]

            for idx in range(0, len(params_list), 100):
                self.experiment.log_batch(run_id=self.run_id, params=params_list[idx : idx + 100])

    @rank_zero_only
    def finalize(self, status: str = "success") -> None:
        # save the last obtained refresh token to disk
        self.auth.save()

        # finalize logging and system metrics monitor
        if getattr(self, "run_id_to_system_metrics_monitor", None):
            self.run_id_to_system_metrics_monitor[self.run_id].finish()
        if getattr(self, "run_id_to_log_monitor", None):
            self.run_id_to_log_monitor[self.run_id].finish(status)

        super().finalize(status)