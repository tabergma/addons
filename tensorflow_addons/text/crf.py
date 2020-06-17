# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import numpy as np
import tensorflow as tf

from tensorflow_addons.utils.types import TensorLike
from typeguard import typechecked
from typing import Optional, Tuple

# TODO: Wrap functions in @tf.function once
# https://github.com/tensorflow/tensorflow/issues/29075 is resolved


def crf_sequence_score(
    inputs: TensorLike,
    tag_indices: TensorLike,
    sequence_lengths: TensorLike,
    transition_params: TensorLike,
) -> tf.Tensor:
    """Computes the unnormalized score for a tag sequence.

    Args:
      inputs: A [batch_size, max_seq_len, num_tags] tensor of unary potentials
          to use as input to the CRF layer.
      tag_indices: A [batch_size, max_seq_len] matrix of tag indices for which
          we compute the unnormalized score.
      sequence_lengths: A [batch_size] vector of true sequence lengths.
      transition_params: A [num_tags, num_tags] transition matrix.
    Returns:
      sequence_scores: A [batch_size] vector of unnormalized sequence scores.
    """
    tag_indices = tf.cast(tag_indices, dtype=tf.int32)
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)

    # If max_seq_len is 1, we skip the score calculation and simply gather the
    # unary potentials of the single tag.
    def _single_seq_fn():
        batch_size = tf.shape(inputs, out_type=tf.int32)[0]
        batch_inds = tf.reshape(tf.range(batch_size), [-1, 1])
        indices = tf.concat([batch_inds, tf.zeros_like(batch_inds)], axis=1)

        tag_inds = tf.gather_nd(tag_indices, indices)
        tag_inds = tf.reshape(tag_inds, [-1, 1])
        indices = tf.concat([indices, tag_inds], axis=1)

        sequence_scores = tf.gather_nd(inputs, indices)

        sequence_scores = tf.where(
            tf.less_equal(sequence_lengths, 0),
            tf.zeros_like(sequence_scores),
            sequence_scores,
        )
        return sequence_scores

    def _multi_seq_fn():
        # Compute the scores of the given tag sequence.
        unary_scores = crf_unary_score(tag_indices, sequence_lengths, inputs)
        binary_scores = crf_binary_score(
            tag_indices, sequence_lengths, transition_params
        )
        sequence_scores = unary_scores + binary_scores
        return sequence_scores

    return tf.cond(tf.equal(tf.shape(inputs)[1], 1), _single_seq_fn, _multi_seq_fn)


def crf_multitag_sequence_score(
    inputs: TensorLike,
    tag_bitmap: TensorLike,
    sequence_lengths: TensorLike,
    transition_params: TensorLike,
) -> tf.Tensor:
    """Computes the unnormalized score of all tag sequences matching
    tag_bitmap.

    tag_bitmap enables more than one tag to be considered correct at each time
    step. This is useful when an observed output at a given time step is
    consistent with more than one tag, and thus the log likelihood of that
    observation must take into account all possible consistent tags.

    Using one-hot vectors in tag_bitmap gives results identical to
    crf_sequence_score.

    Args:
      inputs: A [batch_size, max_seq_len, num_tags] tensor of unary potentials
          to use as input to the CRF layer.
      tag_bitmap: A [batch_size, max_seq_len, num_tags] boolean tensor
          representing all active tags at each index for which to calculate the
          unnormalized score.
      sequence_lengths: A [batch_size] vector of true sequence lengths.
      transition_params: A [num_tags, num_tags] transition matrix.
    Returns:
      sequence_scores: A [batch_size] vector of unnormalized sequence scores.
    """
    tag_bitmap = tf.cast(tag_bitmap, dtype=tf.bool)
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)
    filtered_inputs = tf.where(
        tag_bitmap, inputs, tf.fill(tf.shape(inputs), float("-inf"))
    )

    # If max_seq_len is 1, we skip the score calculation and simply gather the
    # unary potentials of all active tags.
    def _single_seq_fn():
        return tf.reduce_logsumexp(filtered_inputs, axis=[1, 2], keepdims=False)

    def _multi_seq_fn():
        # Compute the logsumexp of all scores of sequences
        # matching the given tags.
        return crf_log_norm(
            inputs=filtered_inputs,
            sequence_lengths=sequence_lengths,
            transition_params=transition_params,
        )

    return tf.cond(tf.equal(tf.shape(inputs)[1], 1), _single_seq_fn, _multi_seq_fn)


def crf_log_norm(
    inputs: TensorLike, sequence_lengths: TensorLike, transition_params: TensorLike
) -> tf.Tensor:
    """Computes the normalization for a CRF.

    Args:
      inputs: A [batch_size, max_seq_len, num_tags] tensor of unary potentials
          to use as input to the CRF layer.
      sequence_lengths: A [batch_size] vector of true sequence lengths.
      transition_params: A [num_tags, num_tags] transition matrix.
    Returns:
      log_norm: A [batch_size] vector of normalizers for a CRF.
    """
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)
    # Split up the first and rest of the inputs in preparation for the forward
    # algorithm.
    first_input = tf.slice(inputs, [0, 0, 0], [-1, 1, -1])
    first_input = tf.squeeze(first_input, [1])

    # If max_seq_len is 1, we skip the algorithm and simply reduce_logsumexp
    # over the "initial state" (the unary potentials).
    def _single_seq_fn():
        log_norm = tf.reduce_logsumexp(first_input, [1])
        # Mask `log_norm` of the sequences with length <= zero.
        log_norm = tf.where(
            tf.less_equal(sequence_lengths, 0), tf.zeros_like(log_norm), log_norm
        )
        return log_norm

    def _multi_seq_fn():
        """Forward computation of alpha values."""
        rest_of_input = tf.slice(inputs, [0, 1, 0], [-1, -1, -1])
        # Compute the alpha values in the forward algorithm in order to get the
        # partition function.

        alphas = crf_forward(
            rest_of_input, first_input, transition_params, sequence_lengths
        )
        log_norm = tf.reduce_logsumexp(alphas, [1])
        # Mask `log_norm` of the sequences with length <= zero.
        log_norm = tf.where(
            tf.less_equal(sequence_lengths, 0), tf.zeros_like(log_norm), log_norm
        )
        return log_norm

    return tf.cond(tf.equal(tf.shape(inputs)[1], 1), _single_seq_fn, _multi_seq_fn)


def crf_log_likelihood(
    inputs: TensorLike,
    tag_indices: TensorLike,
    sequence_lengths: TensorLike,
    transition_params: Optional[TensorLike] = None,
) -> tf.Tensor:
    """Computes the log-likelihood of tag sequences in a CRF.

    Args:
      inputs: A [batch_size, max_seq_len, num_tags] tensor of unary potentials
          to use as input to the CRF layer.
      tag_indices: A [batch_size, max_seq_len] matrix of tag indices for which
          we compute the log-likelihood.
      sequence_lengths: A [batch_size] vector of true sequence lengths.
      transition_params: A [num_tags, num_tags] transition matrix,
          if available.
    Returns:
      log_likelihood: A [batch_size] `Tensor` containing the log-likelihood of
        each example, given the sequence of tag indices.
      transition_params: A [num_tags, num_tags] transition matrix. This is
          either provided by the caller or created in this function.
    """
    num_tags = inputs.shape[2]

    # cast type to handle different types
    tag_indices = tf.cast(tag_indices, dtype=tf.int32)
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)

    if transition_params is None:
        initializer = tf.keras.initializers.GlorotUniform()
        transition_params = tf.Variable(
            initializer([num_tags, num_tags]), "transitions"
        )

    sequence_scores = crf_sequence_score(
        inputs, tag_indices, sequence_lengths, transition_params
    )
    log_norm = crf_log_norm(inputs, sequence_lengths, transition_params)

    # Normalize the scores to get the log-likelihood per example.
    log_likelihood = sequence_scores - log_norm
    return log_likelihood, transition_params


def crf_unary_score(
    tag_indices: TensorLike, sequence_lengths: TensorLike, inputs: TensorLike
) -> tf.Tensor:
    """Computes the unary scores of tag sequences.

    Args:
      tag_indices: A [batch_size, max_seq_len] matrix of tag indices.
      sequence_lengths: A [batch_size] vector of true sequence lengths.
      inputs: A [batch_size, max_seq_len, num_tags] tensor of unary potentials.
    Returns:
      unary_scores: A [batch_size] vector of unary scores.
    """
    tag_indices = tf.cast(tag_indices, dtype=tf.int32)
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)

    batch_size = tf.shape(inputs)[0]
    max_seq_len = tf.shape(inputs)[1]
    num_tags = tf.shape(inputs)[2]

    flattened_inputs = tf.reshape(inputs, [-1])

    offsets = tf.expand_dims(tf.range(batch_size) * max_seq_len * num_tags, 1)
    offsets += tf.expand_dims(tf.range(max_seq_len) * num_tags, 0)
    # Use int32 or int64 based on tag_indices' dtype.
    if tag_indices.dtype == tf.int64:
        offsets = tf.cast(offsets, tf.int64)
    flattened_tag_indices = tf.reshape(offsets + tag_indices, [-1])

    unary_scores = tf.reshape(
        tf.gather(flattened_inputs, flattened_tag_indices), [batch_size, max_seq_len]
    )

    masks = tf.sequence_mask(
        sequence_lengths, maxlen=tf.shape(tag_indices)[1], dtype=tf.float32
    )

    unary_scores = tf.reduce_sum(unary_scores * masks, 1)
    return unary_scores


def crf_binary_score(
    tag_indices: TensorLike, sequence_lengths: TensorLike, transition_params: TensorLike
) -> tf.Tensor:
    """Computes the binary scores of tag sequences.

    Args:
      tag_indices: A [batch_size, max_seq_len] matrix of tag indices.
      sequence_lengths: A [batch_size] vector of true sequence lengths.
      transition_params: A [num_tags, num_tags] matrix of binary potentials.
    Returns:
      binary_scores: A [batch_size] vector of binary scores.
    """
    tag_indices = tf.cast(tag_indices, dtype=tf.int32)
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)

    num_tags = tf.shape(transition_params)[0]
    num_transitions = tf.shape(tag_indices)[1] - 1

    # Truncate by one on each side of the sequence to get the start and end
    # indices of each transition.
    start_tag_indices = tf.slice(tag_indices, [0, 0], [-1, num_transitions])
    end_tag_indices = tf.slice(tag_indices, [0, 1], [-1, num_transitions])

    # Encode the indices in a flattened representation.
    flattened_transition_indices = start_tag_indices * num_tags + end_tag_indices
    flattened_transition_params = tf.reshape(transition_params, [-1])

    # Get the binary scores based on the flattened representation.
    binary_scores = tf.gather(flattened_transition_params, flattened_transition_indices)

    masks = tf.sequence_mask(
        sequence_lengths, maxlen=tf.shape(tag_indices)[1], dtype=tf.float32
    )
    truncated_masks = tf.slice(masks, [0, 1], [-1, -1])
    binary_scores = tf.reduce_sum(binary_scores * truncated_masks, 1)
    return binary_scores


def crf_forward(
    inputs: TensorLike,
    state: TensorLike,
    transition_params: TensorLike,
    sequence_lengths: TensorLike,
) -> tf.Tensor:
    """Computes the alpha values in a linear-chain CRF.

    See http://www.cs.columbia.edu/~mcollins/fb.pdf for reference.

    Args:
      inputs: A [batch_size, num_tags] matrix of unary potentials.
      state: A [batch_size, num_tags] matrix containing the previous alpha
         values.
      transition_params: A [num_tags, num_tags] matrix of binary potentials.
          This matrix is expanded into a [1, num_tags, num_tags] in preparation
          for the broadcast summation occurring within the cell.
      sequence_lengths: A [batch_size] vector of true sequence lengths.

    Returns:
      new_alphas: A [batch_size, num_tags] matrix containing the
          new alpha values.
    """
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)

    last_index = tf.maximum(
        tf.constant(0, dtype=sequence_lengths.dtype), sequence_lengths - 1
    )
    inputs = tf.transpose(inputs, [1, 0, 2])
    transition_params = tf.expand_dims(transition_params, 0)

    def _scan_fn(_state, _inputs):
        _state = tf.expand_dims(_state, 2)
        transition_scores = _state + transition_params
        new_alphas = _inputs + tf.reduce_logsumexp(transition_scores, [1])
        return new_alphas

    all_alphas = tf.transpose(tf.scan(_scan_fn, inputs, state), [1, 0, 2])
    # add first state for sequences of length 1
    all_alphas = tf.concat([tf.expand_dims(state, 1), all_alphas], 1)

    idxs = tf.stack([tf.range(tf.shape(last_index)[0]), last_index], axis=1)
    return tf.gather_nd(all_alphas, idxs)


def viterbi_decode(score: TensorLike, transition_params: TensorLike) -> tf.Tensor:
    """Decode the highest scoring sequence of tags outside of TensorFlow.

    This should only be used at test time.

    Args:
      score: A [seq_len, num_tags] matrix of unary potentials.
      transition_params: A [num_tags, num_tags] matrix of binary potentials.

    Returns:
      viterbi: A [seq_len] list of integers containing the highest scoring tag
          indices.
      viterbi_score: A float containing the score for the Viterbi sequence.
    """
    trellis = np.zeros_like(score)
    backpointers = np.zeros_like(score, dtype=np.int32)
    trellis[0] = score[0]

    for t in range(1, score.shape[0]):
        v = np.expand_dims(trellis[t - 1], 1) + transition_params
        trellis[t] = score[t] + np.max(v, 0)
        backpointers[t] = np.argmax(v, 0)

    viterbi = [np.argmax(trellis[-1])]
    for bp in reversed(backpointers[1:]):
        viterbi.append(bp[viterbi[-1]])
    viterbi.reverse()

    viterbi_score = np.max(trellis[-1])
    return viterbi, viterbi_score


class CrfDecodeForwardRnnCell(tf.keras.layers.AbstractRNNCell):
    """Computes the forward decoding in a linear-chain CRF."""

    @typechecked
    def __init__(self, transition_params: TensorLike, **kwargs) -> None:
        """Initialize the CrfDecodeForwardRnnCell.

        Args:
          transition_params: A [num_tags, num_tags] matrix of binary
            potentials. This matrix is expanded into a
            [1, num_tags, num_tags] in preparation for the broadcast
            summation occurring within the cell.
        """
        super().__init__(**kwargs)
        self._transition_params = tf.expand_dims(transition_params, 0)
        self._num_tags = transition_params.shape[0]

    @property
    def state_size(self) -> int:
        return self._num_tags

    @property
    def output_size(self) -> int:
        return self._num_tags

    def build(self, input_shape):
        super().build(input_shape)

    def call(
        self, inputs: TensorLike, state: TensorLike
    ) -> Tuple[tf.Tensor, tf.Tensor]:
        """Build the CrfDecodeForwardRnnCell.

        Args:
          inputs: A [batch_size, num_tags] matrix of unary potentials.
          state: A [batch_size, num_tags] matrix containing the previous step's
                score values.

        Returns:
          output: A [batch_size, num_tags * 2] matrix of backpointers and scores.
          new_state: A [batch_size, num_tags] matrix of new score values.
        """
        state = tf.expand_dims(state[0], 2)
        transition_scores = state + self._transition_params
        new_state = inputs + tf.reduce_max(transition_scores, [1])

        backpointers = tf.argmax(transition_scores, 1)
        backpointers = tf.cast(backpointers, dtype=tf.float32)

        # apply softmax to transition_scores to get scores in range from 0 to 1
        scores = tf.reduce_max(tf.nn.softmax(transition_scores, axis=1), [1])

        # In the RNN implementation only the first value that is returned from a cell
        # is kept throughout the RNN, so that you will have the values from each time
        # step in the final output. As we need the backpointers as well as the scores
        # for each time step, we concatenate them.
        return tf.concat([backpointers, scores], axis=1), new_state


def crf_decode_forward(
    inputs: TensorLike,
    state: TensorLike,
    transition_params: TensorLike,
    sequence_lengths: TensorLike,
) -> Tuple[tf.Tensor, tf.Tensor]:
    """Computes forward decoding in a linear-chain CRF.

    Args:
      inputs: A [batch_size, num_tags] matrix of unary potentials.
      state: A [batch_size, num_tags] matrix containing the previous step's
            score values.
      transition_params: A [num_tags, num_tags] matrix of binary potentials.
      sequence_lengths: A [batch_size] vector of true sequence lengths.

    Returns:
      output: A [batch_size, num_tags * 2] matrix of backpointers and scores.
      new_state: A [batch_size, num_tags] matrix of new score values.
    """
    sequence_lengths = tf.cast(sequence_lengths, dtype=tf.int32)
    mask = tf.sequence_mask(sequence_lengths, tf.shape(inputs)[1])
    crf_fwd_cell = CrfDecodeForwardRnnCell(transition_params)
    crf_fwd_layer = tf.keras.layers.RNN(
        crf_fwd_cell, return_sequences=True, return_state=True
    )
    return crf_fwd_layer(inputs, state, mask=mask)


def crf_decode_backward(
    backpointers: TensorLike, scores: TensorLike, state: TensorLike
) -> Tuple[tf.Tensor, tf.Tensor]:
    """Computes backward decoding in a linear-chain CRF.

    Args:
      backpointers: A [batch_size, num_tags] matrix of backpointer of next step
            (in time order).
      scores: A [batch_size, num_tags] matrix of scores of next step (in time order).
      state: A [batch_size, 1] matrix of tag index of next step.

    Returns:
      new_tags: A [batch_size, num_tags] tensor containing the new tag indices.
      new_scores: A [batch_size, num_tags] tensor containing the new score values.
    """
    backpointers = tf.transpose(backpointers, [1, 0, 2])
    scores = tf.transpose(scores, [1, 0, 2])

    def _scan_fn(_state: TensorLike, _inputs: TensorLike) -> tf.Tensor:
        _state = tf.cast(tf.squeeze(_state, axis=[1]), dtype=tf.int32)
        idxs = tf.stack([tf.range(tf.shape(_inputs)[0]), _state], axis=1)
        return tf.expand_dims(tf.gather_nd(_inputs, idxs), axis=-1)

    output_tags = tf.scan(_scan_fn, backpointers, state)
    # the dtype of the input parameters of tf.scan need to match
    # convert state to float32 to match the type of scores
    state = tf.cast(state, dtype=tf.float32)
    output_scores = tf.scan(_scan_fn, scores, state)

    return tf.transpose(output_tags, [1, 0, 2]), tf.transpose(output_scores, [1, 0, 2])


def crf_decode(
    potentials: TensorLike, transition_params: TensorLike, sequence_length: TensorLike
) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
    """Decode the highest scoring sequence of tags.

    Args:
      potentials: A [batch_size, max_seq_len, num_tags] tensor of
                unary potentials.
      transition_params: A [num_tags, num_tags] matrix of
                binary potentials.
      sequence_length: A [batch_size] vector of true sequence lengths.

    Returns:
      decode_tags: A [batch_size, max_seq_len] matrix, with dtype `tf.int32`.
                  Contains the highest scoring tag indices.
      scores: A [batch_size, max_seq_len] vector, containing the confidence score of
             `decode_tags`.
      best_score: A [batch_size] vector, containing the score of `decode_tags`.
    """
    sequence_length = tf.cast(sequence_length, dtype=tf.int32)

    # If max_seq_len is 1, we skip the algorithm and simply return the
    # argmax tag and the max activation.
    def _single_seq_fn():
        decode_tags = tf.cast(tf.argmax(potentials, axis=2), dtype=tf.int32)
        decode_scores = tf.reduce_max(tf.nn.softmax(potentials, axis=2), axis=2)
        best_score = tf.reshape(tf.reduce_max(potentials, axis=2), shape=[-1])
        return decode_tags, decode_scores, best_score

    def _multi_seq_fn():
        # Computes forward decoding. Get last score and backpointers.
        initial_state = tf.slice(potentials, [0, 0, 0], [-1, 1, -1])
        initial_state = tf.squeeze(initial_state, axis=[1])
        inputs = tf.slice(potentials, [0, 1, 0], [-1, -1, -1])

        sequence_length_less_one = tf.maximum(
            tf.constant(0, dtype=tf.int32), sequence_length - 1
        )

        output, last_score = crf_decode_forward(
            inputs, initial_state, transition_params, sequence_length_less_one
        )

        # output is a matrix of size [batch-size, max-seq-length, num-tags * 2]
        # split the matrix on axis 2 to get the backpointers and scores, which are
        # both of size [batch-size, max-seq-length, num-tags]
        backpointers, scores = tf.split(output, 2, axis=2)

        backpointers = tf.cast(backpointers, dtype=tf.int32)
        backpointers = tf.reverse_sequence(
            backpointers, sequence_length_less_one, seq_axis=1
        )

        scores = tf.reverse_sequence(scores, sequence_length_less_one, seq_axis=1)

        initial_state = tf.cast(tf.argmax(last_score, axis=1), dtype=tf.int32)
        initial_state = tf.expand_dims(initial_state, axis=-1)

        initial_score = tf.reduce_max(tf.nn.softmax(last_score, axis=1), axis=[1])
        initial_score = tf.expand_dims(initial_score, axis=-1)

        decode_tags, decode_scores = crf_decode_backward(
            backpointers, scores, initial_state
        )

        decode_tags = tf.squeeze(decode_tags, axis=[2])
        decode_tags = tf.concat([initial_state, decode_tags], axis=1)
        decode_tags = tf.reverse_sequence(decode_tags, sequence_length, seq_axis=1)

        decode_scores = tf.squeeze(decode_scores, axis=[2])
        decode_scores = tf.concat([initial_score, decode_scores], axis=1)
        decode_scores = tf.reverse_sequence(decode_scores, sequence_length, seq_axis=1)

        best_score = tf.reduce_max(last_score, axis=1)

        return decode_tags, decode_scores, best_score

    if potentials.shape[1] is not None:
        # shape is statically know, so we just execute
        # the appropriate code path
        if potentials.shape[1] == 1:
            return _single_seq_fn()
        else:
            return _multi_seq_fn()
    else:
        return tf.cond(
            tf.equal(tf.shape(potentials)[1], 1), _single_seq_fn, _multi_seq_fn
        )
