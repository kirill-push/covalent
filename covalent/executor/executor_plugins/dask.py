# Copyright 2021 Agnostiq Inc.
#
# This file is part of Covalent.
#
# Licensed under the Apache License 2.0 (the "License"). A copy of the
# License may be obtained with this software package or at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Use of this file is prohibited except in compliance with the License.
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Module for defining a Dask executor that submits the input python function in a dask cluster
and waits for execution to finish then returns the result.

This is a plugin executor module; it is loaded if found and properly structured.
"""

import os
from typing import Callable, Dict, List, Literal, Optional

from dask.distributed import CancelledError, Client, Future

from covalent._shared_files import TaskRuntimeError, logger

# Relative imports are not allowed in executor plugins
from covalent._shared_files.config import get_config
from covalent._shared_files.exceptions import TaskCancelledError
from covalent.executor.base import AsyncBaseExecutor
from covalent.executor.utils.wrappers import io_wrapper as dask_wrapper

# The plugin class name must be given by the executor_plugin_name attribute:
EXECUTOR_PLUGIN_NAME = "DaskExecutor"

app_log = logger.app_log
log_stack_info = logger.log_stack_info

_EXECUTOR_PLUGIN_DEFAULTS = {
    "log_stdout": "stdout.log",
    "log_stderr": "stderr.log",
    "cache_dir": os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.environ["HOME"], ".cache"), "covalent"
    ),
    "workdir": os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.environ["HOME"], ".cache"),
        "covalent",
        "workdir",
    ),
    "create_unique_workdir": False,
}

# Temporary
_address_client_mapper = {}


class DaskExecutor(AsyncBaseExecutor):
    """
    Dask executor class that submits the input function to a running dask cluster.
    """

    def __init__(
        self,
        scheduler_address: str = "",
        log_stdout: str = "stdout.log",
        log_stderr: str = "stderr.log",
        conda_env: str = "",
        cache_dir: str = "",
        current_env_on_conda_fail: bool = False,
        workdir: str = "",
        create_unique_workdir: Optional[bool] = None,
    ) -> None:
        if not cache_dir:
            cache_dir = _EXECUTOR_PLUGIN_DEFAULTS["cache_dir"]

        if not workdir:
            try:
                workdir = get_config("executors.dask.workdir")
            except KeyError:
                workdir = _EXECUTOR_PLUGIN_DEFAULTS["workdir"]
                debug_msg = f"Couldn't find `executors.dask.workdir` in config, using default value {workdir}."
                app_log.debug(debug_msg)

        if create_unique_workdir is None:
            try:
                create_unique_workdir = get_config("executors.dask.create_unique_workdir")
            except KeyError:
                create_unique_workdir = _EXECUTOR_PLUGIN_DEFAULTS["create_unique_workdir"]
                debug_msg = f"Couldn't find `executors.dask.create_unique_workdir` in config, using default value {create_unique_workdir}."
                app_log.debug(debug_msg)

        super().__init__(
            log_stdout,
            log_stderr,
            cache_dir,
            conda_env,
            current_env_on_conda_fail,
        )

        self.workdir = workdir
        self.create_unique_workdir = create_unique_workdir
        self.scheduler_address = scheduler_address

    async def run(self, function: Callable, args: List, kwargs: Dict, task_metadata: Dict):
        """Submit the function and inputs to the dask cluster"""

        if not self.scheduler_address:
            try:
                self.scheduler_address = get_config("dask.scheduler_address")
            except KeyError as ex:
                app_log.debug(
                    "No dask scheduler address found in config. Address must be set manually."
                )

        if await self.get_cancel_requested():
            app_log.debug("Task has cancelled")
            raise TaskCancelledError

        dispatch_id = task_metadata["dispatch_id"]
        node_id = task_metadata["node_id"]

        dask_client = _address_client_mapper.get(self.scheduler_address)

        if not dask_client:
            dask_client = Client(address=self.scheduler_address, asynchronous=True)
            _address_client_mapper[self.scheduler_address] = dask_client
            await dask_client

        if self.create_unique_workdir:
            current_workdir = os.path.join(self.workdir, dispatch_id, f"node_{node_id}")
        else:
            current_workdir = self.workdir

        future = dask_client.submit(dask_wrapper, function, args, kwargs, current_workdir)
        await self.set_job_handle(future.key)
        app_log.debug(f"Submitted task {node_id} to dask with key {future.key}")

        try:
            result, worker_stdout, worker_stderr, tb = await future
        except CancelledError as e:
            raise TaskCancelledError() from e

        print(worker_stdout, end="", file=self.task_stdout)
        print(worker_stderr, end="", file=self.task_stderr)

        if tb:
            print(tb, end="", file=self.task_stderr)
            raise TaskRuntimeError(tb)

        # FIX: need to get stdout and stderr from dask worker and print them
        return result

    async def cancel(self, task_metadata: Dict, job_handle) -> Literal[True]:
        """
        Cancel the task being executed by the dask executor currently

        Arg(s)
            task_metadata: Metadata associated with the task
            job_handle: Key assigned to the job by Dask

        Return(s)
            True by default
        """
        dask_client = _address_client_mapper.get(self.scheduler_address)

        if not dask_client:
            dask_client = Client(address=self.scheduler_address, asynchronous=True)
            _address_client_mapper[self.scheduler_address] = dask_client
            await dask_client

        fut: Future = Future(key=job_handle, client=dask_client)
        await fut.cancel()
        app_log.debug(f"Cancelled future with key {job_handle}")
        return True
