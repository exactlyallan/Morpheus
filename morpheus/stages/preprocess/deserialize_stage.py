# SPDX-FileCopyrightText: Copyright (c) 2022-2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import typing
import warnings
from functools import partial

import mrc
from mrc.core import operators as ops

import morpheus._lib.stages as _stages
from morpheus.cli.register_stage import register_stage
from morpheus.config import Config
from morpheus.config import PipelineModes
from morpheus.messages import ControlMessage
from morpheus.messages import MessageMeta
from morpheus.messages import MultiMessage
from morpheus.pipeline.multi_message_stage import MultiMessageStage
from morpheus.pipeline.stream_pair import StreamPair

logger = logging.getLogger(__name__)


@register_stage("deserialize", modes=[PipelineModes.FIL, PipelineModes.NLP, PipelineModes.OTHER])
class DeserializeStage(MultiMessageStage):
    """
    Messages are logically partitioned based on the pipeline config's `pipeline_batch_size` parameter.

    This stage deserialize the output of `FileSourceStage`/`KafkaSourceStage` into a `MultiMessage`. This
    should be one of the first stages after the `Source` object.

    Parameters
    ----------
    c : `morpheus.config.Config`
        Pipeline configuration instance.

    """

    def __init__(self,
                 c: Config,
                 *,
                 ensure_sliceable_index: bool = True,
                 message_type: MultiMessage | ControlMessage = MultiMessage,
                 task_type: str = None,
                 task_payload: dict = None):
        super().__init__(c)

        self._batch_size = c.pipeline_batch_size
        self._ensure_sliceable_index = ensure_sliceable_index

        self._max_concurrent = c.num_threads

        # Mark these stages to log timestamps if requested
        self._should_log_timestamps = True

        self._message_type = message_type
        self._task_type = task_type
        self._task_payload = task_payload

        if (self._message_type == ControlMessage):

            if ((self._task_type is None) != (self._task_payload is None)):
                raise ValueError("Both `task_type` and `task_payload` must be specified if either is specified.")
        else:
            if (self._task_type is not None or self._task_payload is not None):
                raise ValueError("Cannot specify `task_type` or `task_payload` for non-control messages.")

    @property
    def name(self) -> str:
        return "deserialize"

    def accepted_types(self) -> typing.Tuple:
        """
        Returns accepted input types for this stage.

        """
        return (MessageMeta, )

    def supports_cpp_node(self):
        # Enable support by default
        return True

    @staticmethod
    def check_slicable_index(x: MessageMeta, ensure_sliceable_index: bool = True):
        if (not x.has_sliceable_index()):
            if (ensure_sliceable_index):
                old_index_name = x.ensure_sliceable_index()

                if (old_index_name):
                    logger.warning(("Incoming MessageMeta does not have a unique and monotonic index. "
                                    "Updating index to be unique. "
                                    "Existing index will be retained in column '%s'"),
                                   old_index_name)

            else:
                warnings.warn(
                    "Detected a non-sliceable index on an incoming MessageMeta. "
                    "Performance when taking slices of messages may be degraded. "
                    "Consider setting `ensure_sliceable_index==True`",
                    RuntimeWarning)

        return x

    @staticmethod
    def process_dataframe_to_multi_message(x: MessageMeta, batch_size: int,
                                           ensure_sliceable_index: bool) -> typing.List[MultiMessage]:
        """
        The deserialization of the cudf is implemented in this function.

        Parameters
        ----------
        x : cudf.DataFrame
            Input rows that needs to be deserilaized.
        batch_size : int
            Batch size.
        ensure_sliceable_index : bool
            Calls `MessageMeta.ensure_sliceable_index()` on incoming messages to ensure unique and monotonic indices.

        """

        x = DeserializeStage.check_slicable_index(x, ensure_sliceable_index)

        full_message = MultiMessage(meta=x)

        # Now break it up by batches
        output = []

        for i in range(0, full_message.mess_count, batch_size):
            output.append(full_message.get_slice(i, min(i + batch_size, full_message.mess_count)))

        return output

    @staticmethod
    def process_dataframe_to_control_message(x: MessageMeta,
                                             batch_size: int,
                                             ensure_sliceable_index: bool,
                                             task_tuple: tuple[str, dict] | None) -> typing.List[ControlMessage]:
        """
        The deserialization of the cudf is implemented in this function.

        Parameters
        ----------
        x : cudf.DataFrame
            Input rows that needs to be deserilaized.
        batch_size : int
            Batch size.
        ensure_sliceable_index : bool
            Calls `MessageMeta.ensure_sliceable_index()` on incoming messages to ensure unique and monotonic indices.

        """

        # Because ControlMessages only have a C++ implementation, we need to import the C++ MessageMeta and use that
        # 100% of the time
        from morpheus._lib.messages import MessageMeta as MessageMetaCpp

        x = DeserializeStage.check_slicable_index(x, ensure_sliceable_index)

        # Now break it up by batches
        output = []

        if (x.count > batch_size):
            df = x.df

            # Break the message meta into smaller chunks
            for i in range(0, x.count, batch_size):

                cm = ControlMessage()

                cm.payload(MessageMetaCpp(df=df.iloc[i:i + batch_size]))

                if (task_tuple is not None):
                    cm.add_task(task_type=task_tuple[0], task=task_tuple[1])

                output.append(cm)
        else:
            cm = ControlMessage()

            cm.payload(MessageMetaCpp(x.df))

            if (task_tuple is not None):
                cm.add_task(task_type=task_tuple[0], task=task_tuple[1])

            output.append(cm)

        return output

    def _build_single(self, builder: mrc.Builder, input_stream: StreamPair) -> StreamPair:

        stream = input_stream[0]

        if self._build_cpp_node():
            stream = _stages.DeserializeStage(builder, self.unique_name, self._batch_size)
        else:

            if (self._message_type == MultiMessage):
                map_func = partial(DeserializeStage.process_dataframe_to_multi_message,
                                   batch_size=self._batch_size,
                                   ensure_sliceable_index=self._ensure_sliceable_index)
            else:

                if (self._task_type is not None and self._task_payload is not None):
                    task_tuple = (self._task_type, self._task_payload)
                else:
                    task_tuple = None

                map_func = partial(DeserializeStage.process_dataframe_to_control_message,
                                   batch_size=self._batch_size,
                                   ensure_sliceable_index=self._ensure_sliceable_index,
                                   task_tuple=task_tuple)

            stream = builder.make_node(self.unique_name, ops.map(map_func), ops.flatten())

        builder.make_edge(input_stream[0], stream)

        return stream, self._message_type
