# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
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

"""PPO in JAX.

Notation:

B, scalar  - batch size
T, scalar  - number of time-steps in a trajectory, or the value of the padded
             time-step dimension.
OBS, tuple - shape of a singular observation from the environment.
             Ex: For CartPole-v0 this is (4,) and Pong-v0 it's (210, 160, 3)
A, scalar  - Number of actions, assuming a discrete space.

Policy and Value function signatures:

Policy            Function :: [B, T] + OBS ->  [B, T, A]
Value             Function :: [B, T] + OBS ->  [B, T, 1]
Policy and Value  Function :: [B, T] + OBS -> ([B, T, A], [B, T, 1])

i.e. the policy net should take a batch of *trajectories* and at each time-step
in each batch deliver a probability distribution over actions.

NOTE: It doesn't return logits, rather the expectation is that it returns
log-probabilities instead.

NOTE: The policy and value functions need to take care to not take into account
future time-steps while deciding the actions (or value) for the current
time-step.

Policy and Value Function produces a tuple of the expected output of a policy
function and a value function.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import os
import pickle
import time

from absl import logging
import gym
from jax import grad
from jax import jit
from jax import lax
from jax import numpy as np
from jax import random as jax_random
import numpy as onp
from tensor2tensor.envs import env_problem
from tensor2tensor.envs import env_problem_utils
from tensor2tensor.trax import layers
from tensor2tensor.trax import optimizers as trax_opt
from tensor2tensor.trax import trax
from tensorflow.io import gfile

DEBUG_LOGGING = False
GAMMA = 0.99
LAMBDA = 0.95
EPSILON = 0.1
EPOCHS = 50  # 100
NUM_OPTIMIZER_STEPS = 100
POLICY_ONLY_NUM_OPTIMIZER_STEPS = 80
VALUE_ONLY_NUM_OPTIMIZER_STEPS = 80
PRINT_EVERY_OPTIMIZER_STEP = 20
BATCH_TRAJECTORIES = 32
POLICY = "categorical-sampling"


def policy_net(rng_key,
               batch_observations_shape,
               num_actions,
               bottom_layers=None):
  """A policy net function."""
  # Use the bottom_layers as the bottom part of the network and just add the
  # required layers on top of it.
  if bottom_layers is None:
    bottom_layers = []

  # NOTE: The LogSoftmax instead of the Softmax.
  bottom_layers.extend([layers.Dense(num_actions), layers.LogSoftmax()])
  net = layers.Serial(*bottom_layers)

  return net.initialize(batch_observations_shape, rng_key), net


def value_net(rng_key,
              batch_observations_shape,
              num_actions,
              bottom_layers=None):
  """A value net function."""
  del num_actions

  if bottom_layers is None:
    bottom_layers = []
  bottom_layers.extend([
      layers.Dense(1),
  ])
  net = layers.Serial(*bottom_layers)
  return net.initialize(batch_observations_shape, rng_key), net


def policy_and_value_net(rng_key,
                         batch_observations_shape,
                         num_actions,
                         bottom_layers_fn=None,
                         two_towers=True):
  """A policy and value net function."""

  # Layers.

  # Now, with the current logits, one head computes action probabilities and the
  # other computes the value function.
  # NOTE: The LogSoftmax instead of the Softmax because of numerical stability.

  net = None
  if not two_towers:
    tower = [] if bottom_layers_fn is None else bottom_layers_fn()
    tower.extend([
        layers.Branch(
            layers.Serial(layers.Dense(num_actions), layers.LogSoftmax()),
            layers.Dense(1))
    ])
    net = layers.Serial(*tower)
  else:
    tower1 = [] if bottom_layers_fn is None else bottom_layers_fn()
    tower2 = [] if bottom_layers_fn is None else bottom_layers_fn()

    tower1.extend([layers.Dense(num_actions), layers.LogSoftmax()])
    tower2.extend([layers.Dense(1)])

    net = layers.Branch(
        layers.Serial(*tower1),
        layers.Serial(*tower2),
    )
  assert net
  return net.initialize(batch_observations_shape, rng_key), net


def optimizer_fun(net_params, step_size=1e-3):
  opt = trax_opt.Adam(step_size=step_size, b1=0.9, b2=0.999, eps=1e-08)
  opt_init = lambda x: (x, opt.tree_init(x))
  opt_update = lambda i, g, s: opt.tree_update(i, g, s[0], s[1])
  get_params = lambda x: x[0]
  opt_state = opt_init(net_params)
  return opt_state, opt_update, get_params


def log_params(params, name="params"):
  """Dumps the params with `logging.error`."""
  for i, param in enumerate(params):
    if not param:
      # Empty tuple.
      continue
    if not isinstance(param, (list, tuple)):
      logging.error("%s[%d] : (%s) = [%s]", name, i, param.shape,
                    onp.array(param))
    else:
      for j, p in enumerate(param):
        logging.error("\t%s[%d, %d] = [%s]", name, i, j, onp.array(p))


# Should this be collect 'n' trajectories, or
# Run the env for 'n' steps and take completed trajectories, or
# Any other option?
# TODO(afrozm): Replace this with EnvProblem?
def collect_trajectories(env,
                         policy_fun,
                         num_trajectories=1,
                         policy="greedy",
                         max_timestep=None,
                         boundary=20,
                         epsilon=0.1,
                         idx=0,
                         rng=None):
  """Collect trajectories with the given policy net and behaviour.

  Args:
    env: A gym env interface, for now this is not-batched.
    policy_fun: observations(B,T+1) -> log-probabs(B,T+1, A) callable.
    num_trajectories: int, number of trajectories.
    policy: string, "greedy", "epsilon-greedy", or "categorical-sampling" i.e.
      how to use the policy_fun to return an action.
    max_timestep: int or None, the index of the maximum time-step at which we
      return the trajectory, None for ending a trajectory only when env returns
      done.
    boundary: int, boundary for padding, used in EnvProblem envs.
    epsilon: float, the epsilon for `epsilon-greedy` policy.
    idx: int, index on the number of times this function is being called
    rng: jax rng, splittable.

  Returns:
    trajectory: list of (observation, action, reward) tuples, where each element
    `i` is a tuple of numpy arrays with shapes as follows:
    observation[i] = (B, T_i + 1)
    action[i] = (B, T_i)
    reward[i] = (B, T_i)
  """

  if isinstance(env, env_problem.EnvProblem):
    # This is an env_problem, run its collect function.
    return env_problem_utils.play_env_problem_with_policy(
        env,
        policy_fun,
        num_trajectories=num_trajectories,
        max_timestep=max_timestep,
        boundary=boundary,
        idx=idx,
        rng=rng)

  trajectories = []

  for t in range(num_trajectories):
    t_start = time.time()
    rewards = []
    actions = []
    done = False

    observation = env.reset()

    # This is currently shaped (1, 1) + OBS, but new observations will keep
    # getting added to it, making it eventually (1, T+1) + OBS
    observation_history = observation[np.newaxis, np.newaxis, :]

    # Run either till we're done OR if max_timestep is defined only till that
    # timestep.
    ts = 0
    while ((not done) and
           (not max_timestep or observation_history.shape[1] < max_timestep)):
      ts_start = time.time()
      # Run the policy, to pick an action, shape is (1, t, A) because
      # observation_history is shaped (1, t) + OBS
      predictions, _, rng = policy_fun(observation_history, rng=rng)

      # We need the predictions for the last time-step, so squeeze the batch
      # dimension and take the last time-step.
      predictions = np.squeeze(predictions, axis=0)[-1]

      # Policy can be run in one of the following ways:
      #  - Greedy
      #  - Epsilon-Greedy
      #  - Categorical-Sampling
      action = None
      if policy == "greedy":
        action = np.argmax(predictions)
      elif policy == "epsilon-greedy":
        # A schedule for epsilon is 1/k where k is the episode number sampled.
        if onp.random.random() < epsilon:
          # Choose an action at random.
          action = onp.random.randint(0, high=len(predictions))
        else:
          # Return the best action.
          action = np.argmax(predictions)
      elif policy == "categorical-sampling":
        # NOTE: The predictions aren't probabilities but log-probabilities
        # instead, since they were computed with LogSoftmax.
        # So just np.exp them to make them probabilities.
        predictions = np.exp(predictions)
        action = onp.argwhere(onp.random.multinomial(1, predictions) == 1)
      else:
        raise ValueError("Unknown policy: %s" % policy)

      # NOTE: Assumption, single batch.
      try:
        action = int(action)
      except TypeError as err:
        # Let's dump some information before we die off.
        logging.error("Cannot convert action into an integer: [%s]", err)
        logging.error("action.shape: [%s]", action.shape)
        logging.error("action: [%s]", action)
        logging.error("predictions.shape: [%s]", predictions.shape)
        logging.error("predictions: [%s]", predictions)
        logging.error("observation_history: [%s]", observation_history)
        raise err

      observation, reward, done, _ = env.step(action)

      # observation is of shape OBS, so add extra dims and concatenate on the
      # time dimension.
      observation_history = np.concatenate(
          [observation_history, observation[np.newaxis, np.newaxis, :]], axis=1)

      rewards.append(reward)
      actions.append(action)

      ts += 1
      logging.vlog(
          2, "  Collected time-step[ %5d] of trajectory[ %5d] in [%0.2f] msec.",
          ts, t, get_time(ts_start))
    logging.vlog(2, " Collected trajectory[ %5d] in [%0.2f] msec.", t,
                 get_time(t_start))

    # This means we are done we're been terminated early.
    assert done or (max_timestep and
                    max_timestep >= observation_history.shape[1])
    # observation_history is (1, T+1) + OBS, lets squeeze out the batch dim.
    observation_history = np.squeeze(observation_history, axis=0)
    trajectories.append(
        (observation_history, np.stack(actions), np.stack(rewards)))

  return trajectories


# This function can probably be simplified, ask how?
# Can we do something much simpler than lax.pad, maybe np.pad?
# Others?


def get_padding_value(dtype):
  """Returns the padding value given a dtype."""
  padding_value = None
  if dtype == np.uint8:
    padding_value = np.uint8(0)
  elif dtype == np.uint16:
    padding_value = np.uint16(0)
  elif dtype == np.float32 or dtype == np.float64:
    padding_value = 0.0
  else:
    padding_value = 0
  assert padding_value is not None
  return padding_value


# TODO(afrozm): Use np.pad instead and make jittable?
def pad_trajectories(trajectories, boundary=20):
  """Pad trajectories to a bucket length that is a multiple of boundary.

  Args:
    trajectories: list[(observation, actions, rewards)], where each observation
      is shaped (t+1,) + OBS and actions & rewards are shaped (t,), with the
      length of the list being B (batch size).
    boundary: int, bucket length, the actions and rewards are padded to integer
      multiples of boundary.

  Returns:
    tuple: (padding lengths, reward_mask, padded_observations, padded_actions,
        padded_rewards) where padded_observations is shaped (B, T+1) + OBS and
        padded_actions, padded_rewards & reward_mask are shaped (B, T).
        Where T is max(t) rounded up to an integer multiple of boundary.
        padded_length is how much padding we've added and
        reward_mask is 1s for actual rewards and 0s for the padding.
  """

  # Let's compute max(t) over all trajectories.
  t_max = max(r.shape[0] for (_, _, r) in trajectories)

  # t_max is rounded to the next multiple of `boundary`
  boundary = int(boundary)
  bucket_length = boundary * int(np.ceil(float(t_max) / boundary))

  # So all obs will be padded to t_max + 1 and actions and rewards to t_max.
  padded_observations = []
  padded_actions = []
  padded_rewards = []
  padded_lengths = []
  reward_masks = []
  for (o, a, r) in trajectories:
    # Determine the amount to pad, this holds true for obs, actions and rewards.
    num_to_pad = bucket_length + 1 - o.shape[0]
    padded_lengths.append(num_to_pad)
    if num_to_pad == 0:
      padded_observations.append(o)
      padded_actions.append(a)
      padded_rewards.append(r)
      reward_masks.append(onp.ones_like(r, dtype=np.int32))
      continue

    # First pad observations.
    padding_config = [(0, num_to_pad, 0)]
    for _ in range(o.ndim - 1):
      padding_config.append((0, 0, 0))
    padding_config = tuple(padding_config)

    padding_value = get_padding_value(o.dtype)
    action_padding_value = get_padding_value(a.dtype)
    reward_padding_value = get_padding_value(r.dtype)

    padded_obs = lax.pad(o, padding_value, padding_config)
    padded_observations.append(padded_obs)

    # Now pad actions and rewards.
    assert a.ndim == 1 and r.ndim == 1
    padding_config = ((0, num_to_pad, 0),)

    padded_action = lax.pad(a, action_padding_value, padding_config)
    padded_actions.append(padded_action)
    padded_reward = lax.pad(r, reward_padding_value, padding_config)
    padded_rewards.append(padded_reward)

    # Also create the mask to use later.
    reward_mask = onp.ones_like(r, dtype=np.int32)
    reward_masks.append(lax.pad(reward_mask, 0, padding_config))

  return padded_lengths, np.stack(reward_masks), np.stack(
      padded_observations), np.stack(padded_actions), np.stack(padded_rewards)


# TODO(afrozm): JAX-ify this, this is too slow for pong.
def rewards_to_go(rewards, mask, gamma=0.99):
  r"""Computes rewards to go.

  Reward to go is defined as follows, the discounted reward that we have to
  yet collect, going forward from this point, i.e.:

  r2g_t = \sum_{l=0}^{\infty} (\gamma^{l} * reward_{t+l})

  Args:
    rewards: np.ndarray of shape (B, T) of rewards.
    mask: np.ndarray of shape (B, T) of mask for the rewards.
    gamma: float, discount factor.

  Returns:
    rewards to go, np.ndarray of shape (B, T).
  """
  B, T = rewards.shape  # pylint: disable=invalid-name,unused-variable

  masked_rewards = rewards * mask  # (B, T)

  # We use the following recurrence relation, derived from the equation above:
  #
  # r2g[t+1] = (r2g[t] - r[t]) / gamma
  #
  # This means we'll need to calculate r2g[0] first and then r2g[1] and so on ..
  #
  # **However** this leads to overflows for long sequences: r2g[t] - r[t] > 0
  # and gamma < 1.0, so the division keeps increasing.
  #
  # So we just run the recurrence in reverse, i.e.
  #
  # r2g[t] = r[t] + (gamma*r2g[t+1])
  #
  # This is much better, but might have lost updates since the (small) rewards
  # at earlier time-steps may get added to a (very?) large sum.

  # Compute r2g_{T-1} at the start and then compute backwards in time.
  r2gs = [masked_rewards[:, -1]]

  # Go from T-2 down to 0.
  for t in reversed(range(T - 1)):
    r2gs.append(masked_rewards[:, t] + (gamma * r2gs[-1]))

  # The list should have length T.
  assert T == len(r2gs)

  # First we stack them in the correct way to make it (B, T), but these are
  # still from newest (T-1) to oldest (0), so then we flip it on time axis.
  return np.flip(np.stack(r2gs, axis=1), axis=1)


@functools.partial(jit, static_argnums=(0,))
def value_loss(value_net_apply,
               value_net_params,
               observations,
               rewards,
               reward_mask,
               gamma=0.99,
               epsilon=0.2,
               value_prediction_old=None,
               rng=None):
  """Computes the value loss.

  Args:
    value_net_apply: value net apply function with signature (params, ndarray of
      shape (B, T+1) + OBS) -> ndarray(B, T+1, 1)
    value_net_params: params of value_net_apply.
    observations: np.ndarray of shape (B, T+1) + OBS
    rewards: np.ndarray of shape (B, T) of rewards.
    reward_mask: np.ndarray of shape (B, T), the mask over rewards.
    gamma: float, discount factor.
    epsilon: float, clip-fraction, used if value_value_prediction_old isn't None
    value_prediction_old: np.ndarray of shape (B, T+1, 1) of value predictions
      using the old parameters. If provided, we incorporate this in the loss as
      well. This is from the OpenAI baselines implementation.
    rng: jax rng, splittable.

  Returns:
    The average L2 value loss, averaged over instances where reward_mask is 1.
  """

  B, T = rewards.shape  # pylint: disable=invalid-name
  assert (B, T + 1) == observations.shape[:2]

  # NOTE: observations is (B, T+1) + OBS, value_prediction is (B, T+1, 1)
  value_prediction = value_net_apply(observations, value_net_params, rng=rng)
  assert (B, T + 1, 1) == value_prediction.shape

  return value_loss_given_predictions(
      value_prediction,
      rewards,
      reward_mask,
      gamma,
      epsilon=epsilon,
      value_prediction_old=value_prediction_old)


@jit
def value_loss_given_predictions(value_prediction,
                                 rewards,
                                 reward_mask,
                                 gamma=0.99,
                                 epsilon=0.2,
                                 value_prediction_old=None):
  """Computes the value loss given the prediction of the value function.

  Args:
    value_prediction: np.ndarray of shape (B, T+1, 1)
    rewards: np.ndarray of shape (B, T) of rewards.
    reward_mask: np.ndarray of shape (B, T), the mask over rewards.
    gamma: float, discount factor.
    epsilon: float, clip-fraction, used if value_value_prediction_old isn't None
    value_prediction_old: np.ndarray of shape (B, T+1, 1) of value predictions
      using the old parameters. If provided, we incorporate this in the loss as
      well. This is from the OpenAI baselines implementation.

  Returns:
    The average L2 value loss, averaged over instances where reward_mask is 1.
  """

  B, T = rewards.shape  # pylint: disable=invalid-name
  assert (B, T) == reward_mask.shape
  assert (B, T + 1, 1) == value_prediction.shape

  value_prediction = np.squeeze(value_prediction, axis=2)  # (B, T+1)
  value_prediction = value_prediction[:, :-1] * reward_mask  # (B, T)
  r2g = rewards_to_go(rewards, reward_mask, gamma=gamma)  # (B, T)
  loss = (value_prediction - r2g)**2

  # From the baselines implementation.
  if value_prediction_old is not None:
    value_prediction_old = np.squeeze(value_prediction_old, axis=2)  # (B, T+1)
    value_prediction_old = value_prediction_old[:, :-1] * reward_mask  # (B, T)

    v_clipped = value_prediction_old + np.clip(
        value_prediction - value_prediction_old, -epsilon, epsilon)
    v_clipped_loss = (v_clipped - r2g)**2
    loss = np.maximum(v_clipped_loss, loss)

  # Take an average on only the points where mask != 0.
  return np.sum(loss) / np.sum(reward_mask)


# TODO(afrozm): JAX-ify this, this is too slow for pong.
def deltas(predicted_values, rewards, mask, gamma=0.99):
  r"""Computes TD-residuals from V(s) and rewards.

  Where a `delta`, i.e. a td-residual is defined as:

  delta_{b,t} = r_{b,t} + \gamma * v_{b,t+1} - v_{b,t}.

  Args:
    predicted_values: ndarray of shape (B, T+1). NOTE: Expects axis 2 was
      squeezed. These represent V(s_bt) for b < B and t < T+1
    rewards: ndarray of shape (B, T) of rewards.
    mask: ndarray of shape (B, T) of mask for rewards.
    gamma: float, discount factor.

  Returns:
    ndarray of shape (B, T) of one-step TD-residuals.
  """

  # `d`s are basically one-step TD residuals.
  d = []
  _, T = rewards.shape  # pylint: disable=invalid-name
  for t in range(T):
    d.append(rewards[:, t] + (gamma * predicted_values[:, t + 1]) -
             predicted_values[:, t])

  return np.array(d).T * mask


def gae_advantages(td_deltas, mask, lambda_=0.95, gamma=0.99):
  r"""Computes the GAE advantages given the one step TD-residuals.

  The formula for a GAE advantage estimator is as follows:

  A_{bt} = \sum_{l=0}^{\infty}(\gamma * \lambda)^{l}(\delta_{b,t+l}).

  Internally we just call rewards_to_go, since it is the same computation.

  Args:
    td_deltas: np.ndarray of shape (B, T) of one step TD-residuals.
    mask: np.ndarray of shape (B, T) of mask for the residuals. It maybe the
      case that the `td_deltas` are already masked correctly since they are
      produced by `deltas(...)`
    lambda_: float, lambda parameter for GAE estimators.
    gamma: float, lambda parameter for GAE estimators.

  Returns:
    GAE advantage estimates.
  """

  return rewards_to_go(td_deltas, mask, lambda_ * gamma)


def chosen_probabs(probab_observations, actions):
  """Picks out the probabilities of the actions along batch and time-steps.

  Args:
    probab_observations: ndarray of shape `[B, T+1, A]`, where
      probab_observations[b, t, i] contains the log-probability of action = i at
      the t^th time-step in the b^th trajectory.
    actions: ndarray of shape `[B, T]`, with each entry in [0, A) denoting which
      action was chosen in the b^th trajectory's t^th time-step.

  Returns:
    `[B, T]` ndarray with the log-probabilities of the chosen actions.
  """
  B, T = actions.shape  # pylint: disable=invalid-name
  assert (B, T + 1) == probab_observations.shape[:2]
  return probab_observations[np.arange(B)[:, None], np.arange(T), actions]


def compute_probab_ratios(p_new, p_old, actions, reward_mask):
  """Computes the probability ratios for each time-step in a trajectory.

  Args:
    p_new: ndarray of shape [B, T+1, A] of the log-probabilities that the policy
      network assigns to all the actions at each time-step in each batch using
      the old parameters.
    p_old: ndarray of shape [B, T+1, A], same as above, but using old policy
      network parameters.
    actions: ndarray of shape [B, T] where each element is from [0, A).
    reward_mask: ndarray of shape [B, T] masking over probabilities.

  Returns:
    probab_ratios: ndarray of shape [B, T], where
    probab_ratios_{b,t} = p_new_{b,t,action_{b,t}} / p_old_{b,t,action_{b,t}}
  """

  B, T = actions.shape  # pylint: disable=invalid-name
  assert (B, T + 1) == p_old.shape[:2]
  assert (B, T + 1) == p_new.shape[:2]

  logp_old = chosen_probabs(p_old, actions)
  logp_new = chosen_probabs(p_new, actions)

  assert (B, T) == logp_old.shape
  assert (B, T) == logp_new.shape

  # Since these are log-probabilities, we just subtract them.
  probab_ratios = np.exp(logp_new - logp_old) * reward_mask
  assert (B, T) == probab_ratios.shape
  return probab_ratios


def clipped_probab_ratios(probab_ratios, epsilon=0.2):
  return np.clip(probab_ratios, 1 - epsilon, 1 + epsilon)


def clipped_objective(probab_ratios, advantages, reward_mask, epsilon=0.2):
  return np.minimum(
      probab_ratios * advantages,
      clipped_probab_ratios(probab_ratios, epsilon=epsilon) *
      advantages) * reward_mask


@functools.partial(jit, static_argnums=(0,))
def ppo_loss(policy_net_apply,
             new_policy_params,
             log_probab_actions_old,
             value_predictions_old,
             padded_observations,
             padded_actions,
             padded_rewards,
             reward_mask,
             gamma=0.99,
             lambda_=0.95,
             epsilon=0.2,
             rng=None):
  """PPO objective, with an eventual minus sign, given observations."""
  B, T = padded_rewards.shape  # pylint: disable=invalid-name
  assert (B, T + 1) == padded_observations.shape[:2]
  assert (B, T + 1) == log_probab_actions_old.shape[:2]
  assert (B, T + 1, 1) == value_predictions_old.shape
  assert (B, T) == padded_actions.shape
  assert (B, T) == padded_rewards.shape
  assert (B, T) == reward_mask.shape

  # Compute predicted log-probs and hand over to `ppo_loss_given_predictions`.

  # log_probab_actions_{old,new} are both (B, T+1, A)
  log_probab_actions_new = policy_net_apply(
      padded_observations, new_policy_params, rng=rng)
  assert (B, T + 1) == log_probab_actions_new.shape[:2]
  assert log_probab_actions_old.shape[-1] == log_probab_actions_new.shape[-1]

  return ppo_loss_given_predictions(
      log_probab_actions_new,
      log_probab_actions_old,
      value_predictions_old,
      padded_actions,
      padded_rewards,
      reward_mask,
      gamma=gamma,
      lambda_=lambda_,
      epsilon=epsilon)


@jit
def ppo_loss_given_predictions(log_probab_actions_new,
                               log_probab_actions_old,
                               value_predictions_old,
                               padded_actions,
                               padded_rewards,
                               reward_mask,
                               gamma=0.99,
                               lambda_=0.95,
                               epsilon=0.2):
  """PPO objective, with an eventual minus sign, given predictions."""
  B, T = padded_rewards.shape  # pylint: disable=invalid-name
  assert (B, T) == padded_actions.shape
  assert (B, T) == reward_mask.shape

  _, _, A = log_probab_actions_old.shape  # pylint: disable=invalid-name
  assert (B, T + 1, 1) == value_predictions_old.shape
  assert (B, T + 1, A) == log_probab_actions_old.shape
  assert (B, T + 1, A) == log_probab_actions_new.shape

  # (B, T)
  td_deltas = deltas(
      np.squeeze(value_predictions_old, axis=2),  # (B, T+1)
      padded_rewards,
      reward_mask,
      gamma=gamma)

  # (B, T)
  advantages = gae_advantages(
      td_deltas, reward_mask, lambda_=lambda_, gamma=gamma)

  # Normalize the advantages.
  advantages = (advantages - np.mean(advantages)) / np.std(advantages)

  # (B, T)
  ratios = compute_probab_ratios(log_probab_actions_new, log_probab_actions_old,
                                 padded_actions, reward_mask)
  assert (B, T) == ratios.shape

  # (B, T)
  objective = clipped_objective(
      ratios, advantages, reward_mask, epsilon=epsilon)
  assert (B, T) == objective.shape

  # ()
  average_objective = np.sum(objective) / np.sum(reward_mask)

  # Loss is negative objective.
  return -average_objective


@jit
def combined_loss_given_predictions(log_probab_actions_new,
                                    log_probab_actions_old,
                                    value_prediction_new,
                                    value_prediction_old,
                                    padded_actions,
                                    padded_rewards,
                                    reward_mask,
                                    gamma=0.99,
                                    lambda_=0.95,
                                    epsilon=0.2,
                                    c1=1.0,
                                    c2=0.01):
  """Computes the combined (clipped loss + value loss) given predictions."""
  loss_value = value_loss_given_predictions(
      value_prediction_new,
      padded_rewards,
      reward_mask,
      gamma=gamma,
      value_prediction_old=value_prediction_old,
      epsilon=epsilon)
  loss_ppo = ppo_loss_given_predictions(
      log_probab_actions_new,
      log_probab_actions_old,
      value_prediction_old,
      padded_actions,
      padded_rewards,
      reward_mask,
      gamma=gamma,
      lambda_=lambda_,
      epsilon=epsilon)
  entropy_bonus = masked_entropy(log_probab_actions_new, reward_mask)
  return (loss_ppo + (c1 * loss_value) - (c2 * entropy_bonus), loss_ppo,
          loss_value, entropy_bonus)


@functools.partial(jit, static_argnums=(3,))
def combined_loss(new_params,
                  log_probab_actions_old,
                  value_predictions_old,
                  policy_and_value_net_apply,
                  padded_observations,
                  padded_actions,
                  padded_rewards,
                  reward_mask,
                  gamma=0.99,
                  lambda_=0.95,
                  epsilon=0.2,
                  c1=1.0,
                  c2=0.01,
                  rng=None):
  """Computes the combined (clipped loss + value loss) given observations."""
  log_probab_actions_new, value_predictions_new = policy_and_value_net_apply(
      padded_observations, new_params, rng=rng)

  # (combined_loss, ppo_loss, value_loss, entropy_bonus)
  return combined_loss_given_predictions(
      log_probab_actions_new,
      log_probab_actions_old,
      value_predictions_new,
      value_predictions_old,
      padded_actions,
      padded_rewards,
      reward_mask,
      gamma=gamma,
      lambda_=lambda_,
      epsilon=epsilon,
      c1=c1,
      c2=c2)


@functools.partial(jit, static_argnums=(2, 3, 4))
def ppo_opt_step(i,
                 opt_state,
                 ppo_opt_update,
                 ppo_get_params,
                 policy_net_apply,
                 log_probab_actions_old,
                 value_predictions_old,
                 padded_observations,
                 padded_actions,
                 padded_rewards,
                 reward_mask,
                 gamma=0.99,
                 lambda_=0.95,
                 epsilon=0.1,
                 rng=None):
  """PPO optimizer step."""
  new_policy_params = ppo_get_params(opt_state)
  g = grad(
      ppo_loss, argnums=1)(
          policy_net_apply,
          new_policy_params,
          log_probab_actions_old,
          value_predictions_old,
          padded_observations,
          padded_actions,
          padded_rewards,
          reward_mask,
          gamma=gamma,
          lambda_=lambda_,
          epsilon=epsilon,
          rng=rng)
  return ppo_opt_update(i, g, opt_state)


@functools.partial(jit, static_argnums=(2, 3, 4))
def value_opt_step(i,
                   opt_state,
                   opt_update,
                   get_params,
                   value_net_apply,
                   padded_observations,
                   padded_rewards,
                   reward_mask,
                   gamma=0.99,
                   rng=None):
  """Value optimizer step."""
  value_params = get_params(opt_state)
  # Note this partial application here and argnums above in ppo_opt_step.
  g = grad(functools.partial(value_loss, value_net_apply))(
      value_params,
      padded_observations,
      padded_rewards,
      reward_mask,
      gamma=gamma,
      rng=rng)
  return opt_update(i, g, opt_state)


@functools.partial(jit, static_argnums=(2, 3, 4))
def policy_and_value_opt_step(i,
                              opt_state,
                              opt_update,
                              get_params,
                              policy_and_value_net_apply,
                              log_probab_actions_old,
                              value_predictions_old,
                              padded_observations,
                              padded_actions,
                              padded_rewards,
                              reward_mask,
                              c1=1.0,
                              c2=0.01,
                              gamma=0.99,
                              lambda_=0.95,
                              epsilon=0.1,
                              rng=None):
  """Policy and Value optimizer step."""

  # Combined loss function given the new params.
  def policy_and_value_loss(params):
    """Returns the combined loss given just parameters."""
    (loss, _, _, _) = combined_loss(
        params,
        log_probab_actions_old,
        value_predictions_old,
        policy_and_value_net_apply,
        padded_observations,
        padded_actions,
        padded_rewards,
        reward_mask,
        c1=c1,
        c2=c2,
        gamma=gamma,
        lambda_=lambda_,
        epsilon=epsilon,
        rng=rng)
    return loss

  new_params = get_params(opt_state)
  g = grad(policy_and_value_loss)(new_params)
  # TODO(afrozm): Maybe clip gradients?
  return opt_update(i, g, opt_state)


def get_time(t1, t2=None):
  if t2 is None:
    t2 = time.time()
  return round((t2 - t1) * 1000, 2)


def approximate_kl(log_prob_new, log_prob_old, mask):
  """Computes the approximate KL divergence between the old and new log-probs.

  Args:
    log_prob_new: (B, T+1, A) log probs new
    log_prob_old: (B, T+1, A) log probs old
    mask: (B, T)

  Returns:
    Approximate KL.
  """
  diff = log_prob_old - log_prob_new
  # Cut the last time-step out.
  diff = diff[:, :-1]
  # Mask out the irrelevant part.
  diff *= mask[:, :, np.newaxis]  # make mask (B, T, 1)
  # Average on non-masked part.
  return np.sum(diff) / np.sum(mask)


def masked_entropy(log_probs, mask):
  """Computes the entropy for the given log-probs.

  Args:
    log_probs: (B, T+1, A) log probs
    mask: (B, T) mask.

  Returns:
    Entropy.
  """
  # Cut the last time-step out.
  lp = log_probs[:, :-1]
  # Mask out the irrelevant part.
  lp *= mask[:, :, np.newaxis]  # make mask (B, T, 1)
  p = np.exp(lp) * mask[:, :, np.newaxis]  # (B, T, 1)
  # Average on non-masked part and take negative.
  return -(np.sum(lp * p) / np.sum(mask))


def evaluate_policy(eval_env,
                    get_predictions,
                    boundary,
                    max_timestep=20000,
                    rng=None):
  """Evaluate the policy."""

  avg_rewards = []
  for policy in [env_problem_utils.CATEGORICAL_SAMPLING,
                 env_problem_utils.GUMBEL_SAMPLING,
                 env_problem_utils.EPSILON_GREEDY]:
    trajs = env_problem_utils.play_env_problem_with_policy(
        eval_env,
        get_predictions,
        boundary=boundary,
        max_timestep=max_timestep,
        idx=0,  # reset always
        policy_sampling=policy,
        rng=rng)
    avg_rewards.append(
        float(sum(np.sum(traj[2]) for traj in trajs)) / len(trajs))
  return tuple(avg_rewards)


def training_loop(
    env=None,
    epochs=EPOCHS,
    policy_net_fun=None,
    value_net_fun=None,
    policy_and_value_net_fun=None,
    policy_optimizer_fun=None,
    value_optimizer_fun=None,
    policy_and_value_optimizer_fun=None,
    batch_size=BATCH_TRAJECTORIES,
    num_optimizer_steps=NUM_OPTIMIZER_STEPS,
    policy_only_num_optimizer_steps=POLICY_ONLY_NUM_OPTIMIZER_STEPS,
    value_only_num_optimizer_steps=VALUE_ONLY_NUM_OPTIMIZER_STEPS,
    print_every_optimizer_steps=PRINT_EVERY_OPTIMIZER_STEP,
    target_kl=0.01,
    boundary=20,
    max_timestep=None,
    max_timestep_eval=20000,
    random_seed=None,
    gamma=GAMMA,
    lambda_=LAMBDA,
    epsilon=EPSILON,
    c1=1.0,
    c2=0.01,
    output_dir=None,
    eval_every_n=1000,
    eval_env=None,
    enable_early_stopping=True):
  """Runs the training loop for PPO, with fixed policy and value nets."""
  assert env

  if output_dir:
    gfile.makedirs(output_dir)

  jax_rng_key = trax.get_random_number_generator_and_set_seed(random_seed)

  value_losses = []
  ppo_objective = []
  combined_losses = []
  average_rewards = []
  eval_average_rewards = []

  # Batch Observations Shape = [-1, -1] + OBS, because we will eventually call
  # policy and value networks on shape [B, T] +_OBS
  batch_observations_shape = (-1, -1) + env.observation_space.shape

  assert isinstance(env.action_space, gym.spaces.Discrete)
  num_actions = env.action_space.n

  policy_and_value_net_params, policy_and_value_net_apply = None, None
  policy_and_value_opt_state, policy_and_value_opt_update = None, None
  policy_net_params, policy_net_apply = None, None
  value_net_params, value_net_apply = None, None
  if policy_and_value_net_fun is not None:
    jax_rng_key, key1, key2 = jax_random.split(jax_rng_key, num=3)

    # Initialize the policy and value network.
    policy_and_value_net_params, policy_and_value_net_apply = (
        policy_and_value_net_fun(key1, batch_observations_shape, num_actions))

    policy_and_value_net_apply = jit(policy_and_value_net_apply)

    # Initialize the optimizers.
    policy_and_value_optimizer = (
        policy_and_value_optimizer_fun(policy_and_value_net_params))
    (policy_and_value_opt_state, policy_and_value_opt_update,
     policy_and_value_get_params) = policy_and_value_optimizer
  else:
    # Initialize the policy and value functions.
    assert policy_net_fun and value_net_fun
    jax_rng_key, key1, key2 = jax_random.split(jax_rng_key, num=3)

    policy_net_params, policy_net_apply = policy_net_fun(
        key1, batch_observations_shape, num_actions)
    value_net_params, value_net_apply = value_net_fun(key2,
                                                      batch_observations_shape,
                                                      num_actions)

    policy_net_apply = jit(policy_net_apply)
    value_net_apply = jit(value_net_apply)

    # Initialize the optimizers.
    ppo_opt_state, ppo_opt_update, ppo_get_params = (
        policy_optimizer_fun(policy_net_params))
    value_opt_state, value_opt_update, value_get_params = (
        value_optimizer_fun(value_net_params))

  for i in range(epochs):

    # Params we'll use to collect the trajectories.
    if policy_and_value_net_apply:
      policy_and_value_net_params = policy_and_value_get_params(
          policy_and_value_opt_state)
    else:
      policy_net_params = ppo_get_params(ppo_opt_state)
      value_net_params = value_get_params(value_opt_state)

    # A function to get the policy and value predictions.
    def get_predictions(observations, rng=None):
      """Returns log-probs, value predictions and key back."""
      key, key1, key2 = jax_random.split(rng, num=3)

      if policy_net_apply is not None:
        return (policy_net_apply(observations, policy_net_params, rng=key1),
                value_net_apply(observations, value_net_params, rng=key2), key)

      assert policy_and_value_net_apply

      log_probs, value_preds = policy_and_value_net_apply(
          observations, policy_and_value_net_params, rng=key1)

      return log_probs, value_preds, key

    # Save params and evaluate the policy.
    if output_dir and (i % eval_every_n == 0):
      jax_rng_key, key = jax_random.split(jax_rng_key, num=2)

      logging.vlog(1, "Epoch [% 6d] saving model and evaluating policy.", i)
      params_file = os.path.join(output_dir, "model-%06d.pkl" % i)
      eval_rewards_file = os.path.join(output_dir, "eval_average_rewards")
      with gfile.GFile(params_file, "wb") as f:
        if policy_and_value_net_params:
          pickle.dump(policy_and_value_net_params, f)
        else:
          pickle.dump((policy_net_params, value_net_params), f)

      # TODO(afrozm): Dump in jaxboard or somewhere?
      avg_reward = evaluate_policy(eval_env, get_predictions, boundary,
                                   max_timestep=max_timestep_eval, rng=key)
      eval_average_rewards.append(avg_reward)
      logging.info("Epoch [% 6d] Policy Evaluation = (%10.2f, %10.2f, %10.2f)",
                   i, avg_reward[0], avg_reward[1], avg_reward[2])
      with gfile.GFile(eval_rewards_file, "w") as f:
        f.write(", ".join([str(r) for r in eval_average_rewards]) + "\n")

    t = time.time()
    t0 = t
    logging.vlog(1, "Epoch [% 6d] collecting trajectories.", i)
    jax_rng_key, key = jax_random.split(jax_rng_key)
    trajs = collect_trajectories(
        env,
        policy_fun=get_predictions,
        num_trajectories=batch_size,
        policy=POLICY,
        max_timestep=max_timestep,
        boundary=boundary,
        rng=key,
        idx=i,
        epsilon=(10.0 / (i + 10.0)))  # this is a different epsilon.

    logging.vlog(1, "Collecting trajectories took %0.2f msec.", get_time(t))

    avg_reward = float(sum(np.sum(traj[2]) for traj in trajs)) / len(trajs)
    max_reward = max(np.sum(traj[2]) for traj in trajs)
    min_reward = min(np.sum(traj[2]) for traj in trajs)
    average_rewards.append(avg_reward)

    logging.vlog(1, "Rewards avg=[%0.2f], max=[%0.2f], min=[%0.2f], all=%s",
                 avg_reward, max_reward, min_reward,
                 [float(np.sum(traj[2])) for traj in trajs])
    logging.vlog(1, "Average Rewards:\n%s", average_rewards)

    # TODO(afrozm): Dump in jaxboard or somewhere?
    if output_dir:
      with gfile.GFile(os.path.join(output_dir, "average_rewards"), "w") as f:
        f.write(", ".join([str(r) for r in average_rewards]) + "\n")

    logging.vlog(1,
                 "Trajectory Length average=[%0.2f], max=[%0.2f], min=[%0.2f]",
                 float(sum(len(traj[0]) for traj in trajs)) / len(trajs),
                 max(len(traj[0]) for traj in trajs),
                 min(len(traj[0]) for traj in trajs))
    logging.vlog(2, "Trajectory Lengths: %s", [len(traj[0]) for traj in trajs])

    t = time.time()
    (_, reward_mask, padded_observations, padded_actions,
     padded_rewards) = pad_trajectories(
         trajs, boundary=boundary)

    logging.vlog(1, "Padding trajectories took %0.2f msec.", get_time(t))
    logging.vlog(1, "Padded Observations' shape [%s]",
                 str(padded_observations.shape))
    logging.vlog(1, "Padded Actions' shape [%s]", str(padded_actions.shape))
    logging.vlog(1, "Padded Rewards' shape [%s]", str(padded_rewards.shape))

    # Calculate log-probabilities and value predictions of the trajectories.
    # We'll pass these to the loss functions so as to not get recomputed.

    # NOTE:
    # There is a slight problem here, if the policy network contains
    # stochasticity in the log-probabilities (ex: dropout), then calculating
    # these again here is not going to be correct and should be done in the
    # collect function.

    jax_rng_key, key = jax_random.split(jax_rng_key)
    log_probabs_traj, value_predictions_traj, _ = get_predictions(
        padded_observations, rng=key)

    # Some assertions.
    B, T = padded_actions.shape  # pylint: disable=invalid-name
    assert (B, T) == padded_rewards.shape
    assert (B, T) == reward_mask.shape
    assert (B, T + 1) == padded_observations.shape[:2]
    assert (B, T + 1) + env.observation_space.shape == padded_observations.shape

    # Linear annealing from 0.1 to 0.0
    # epsilon_schedule = epsilon if epochs == 1 else epsilon * (1.0 -
    #                                                           (i /
    #                                                            (epochs - 1)))

    # Constant epsilon.
    epsilon_schedule = epsilon

    # Compute value and ppo losses.
    cur_value_loss, cur_ppo_loss, cur_combined_loss = None, None, None
    jax_rng_key, key1, key2 = jax_random.split(jax_rng_key, num=3)
    if policy_and_value_net_apply:
      logging.vlog(2, "Starting to compute P&V loss.")
      t = time.time()
      cur_combined_loss, cur_ppo_loss, cur_value_loss, entropy_bonus = (
          combined_loss(
              policy_and_value_net_params,
              log_probabs_traj,
              value_predictions_traj,
              policy_and_value_net_apply,
              padded_observations,
              padded_actions,
              padded_rewards,
              reward_mask,
              gamma=gamma,
              lambda_=lambda_,
              epsilon=epsilon_schedule,
              c1=c1,
              c2=c2,
              rng=key1))
      logging.vlog(
          1,
          "Calculating P&V loss [%10.2f(%10.2f, %10.2f, %10.2f)] took %0.2f msec.",
          cur_combined_loss, cur_value_loss, cur_ppo_loss, entropy_bonus,
          get_time(t))
    else:
      logging.vlog(2, "Starting to compute Value loss.")
      t = time.time()
      cur_value_loss = value_loss(
          value_net_apply,
          value_net_params,
          padded_observations,
          padded_rewards,
          reward_mask,
          gamma=gamma,
          rng=key1)

      logging.vlog(1, "Calculating value loss took %0.2f msec.", get_time(t))

      t = time.time()
      logging.vlog(2, "Starting to compute PPO loss.")
      cur_ppo_loss = ppo_loss(
          policy_net_apply,
          policy_net_params,
          log_probabs_traj,
          value_predictions_traj,
          padded_observations,
          padded_actions,
          padded_rewards,
          reward_mask,
          gamma=gamma,
          lambda_=lambda_,
          epsilon=epsilon_schedule,
          rng=key2)
      logging.vlog(1, "Calculating PPO loss took %0.2f msec.", get_time(t))

    value_losses.append(cur_value_loss)
    ppo_objective.append(-1.0 * cur_ppo_loss)
    if cur_combined_loss:
      combined_losses.append(cur_combined_loss)

    jax_rng_key, key1, key2 = jax_random.split(jax_rng_key, num=3)
    if policy_and_value_net_apply:
      logging.vlog(1, "Policy and Value Optimization")
      t1 = time.time()
      keys = jax_random.split(key1, num=num_optimizer_steps)
      for j in range(num_optimizer_steps):
        k1, k2, k3 = jax_random.split(keys[j], num=3)
        t = time.time()
        # Update the optimizer state.
        policy_and_value_opt_state = policy_and_value_opt_step(
            j,
            policy_and_value_opt_state,
            policy_and_value_opt_update,
            policy_and_value_get_params,
            policy_and_value_net_apply,
            log_probabs_traj,
            value_predictions_traj,
            padded_observations,
            padded_actions,
            padded_rewards,
            reward_mask,
            c1=c1,
            c2=c2,
            gamma=gamma,
            lambda_=lambda_,
            epsilon=epsilon_schedule,
            rng=k1)

        # Compute the approx KL for early stopping.
        new_policy_and_value_net_params = policy_and_value_get_params(
            policy_and_value_opt_state)

        log_probab_actions_new, _ = policy_and_value_net_apply(
            padded_observations, new_policy_and_value_net_params, rng=k2)

        approx_kl = approximate_kl(log_probab_actions_new, log_probabs_traj,
                                   reward_mask)

        early_stopping = enable_early_stopping and approx_kl > 1.5 * target_kl
        if early_stopping:
          logging.vlog(
              1, "Early stopping policy and value optimization at iter: %d, "
              "with approx_kl: %0.2f", j, approx_kl)
          # We don't return right-away, we want the below to execute on the last
          # iteration.

        t2 = time.time()
        if (((j + 1) % print_every_optimizer_steps == 0) or
            (j == num_optimizer_steps - 1) or early_stopping):
          # Compute and log the loss.
          (loss_combined, loss_ppo, loss_value, entropy_bonus) = (
              combined_loss(
                  new_policy_and_value_net_params,
                  log_probabs_traj,
                  value_predictions_traj,
                  policy_and_value_net_apply,
                  padded_observations,
                  padded_actions,
                  padded_rewards,
                  reward_mask,
                  gamma=gamma,
                  lambda_=lambda_,
                  epsilon=epsilon_schedule,
                  c1=c1,
                  c2=c2,
                  rng=k3))
          logging.vlog(1, "One Policy and Value grad desc took: %0.2f msec",
                       get_time(t, t2))
          logging.vlog(
              1, "Combined Loss(value, ppo, entropy_bonus) [%10.2f] ->"
              " [%10.2f(%10.2f,%10.2f,%10.2f)]", cur_combined_loss,
              loss_combined, loss_value, loss_ppo, entropy_bonus)

        if early_stopping:
          break

      logging.vlog(
          1, "Total Combined Loss reduction [%0.2f]%%",
          (100 *
           (cur_combined_loss - loss_combined) / np.abs(cur_combined_loss)))

      logging.info(
          "Epoch [% 6d], Reward[min, max, avg] [%5.2f,%5.2f,%5.2f], Combined"
          " Loss(value, ppo, entropy) [%2.5f(%2.5f,%2.5f,%2.5f)], took [%2.5f msec], Average Rewards(last 10):%s",
          i, min_reward, max_reward,
          avg_reward, loss_combined, loss_value, loss_ppo, entropy_bonus,
          get_time(t1), ", ".join([str(a) for a in average_rewards[-10:]]))
    else:
      # Run optimizers.
      logging.vlog(1, "PPO Optimization")
      t1 = time.time()
      keys1 = jax_random.split(key1, num=policy_only_num_optimizer_steps)
      for j in range(policy_only_num_optimizer_steps):
        k1, k2, k3 = jax_random.split(keys1[j], num=3)
        t = time.time()
        # Update the optimizer state.
        ppo_opt_state = ppo_opt_step(
            j,
            ppo_opt_state,
            ppo_opt_update,
            ppo_get_params,
            policy_net_apply,
            log_probabs_traj,
            value_predictions_traj,
            padded_observations,
            padded_actions,
            padded_rewards,
            reward_mask,
            gamma=gamma,
            lambda_=lambda_,
            epsilon=epsilon_schedule,
            rng=k1,
        )
        t2 = time.time()
        # Compute the approx KL for early stopping.
        # Get the new params.
        new_policy_net_params = ppo_get_params(ppo_opt_state)
        log_probab_actions_new = policy_net_apply(
            padded_observations, new_policy_net_params, rng=k2)
        approx_kl = approximate_kl(log_probab_actions_new, log_probabs_traj,
                                   reward_mask)

        early_stopping = enable_early_stopping and approx_kl > 1.5 * target_kl
        if early_stopping:
          logging.vlog(
              1, "Early stopping policy optimization at iter: %d, "
              "with approx_kl: %0.2f", j, approx_kl)
          # We don't return right-away, we want the below to execute on the last
          # iteration.

        if (((j + 1) % print_every_optimizer_steps == 0) or
            (j == num_optimizer_steps - 1) or early_stopping):
          new_ppo_loss = ppo_loss(
              policy_net_apply,
              new_policy_net_params,
              log_probabs_traj,
              value_predictions_traj,
              padded_observations,
              padded_actions,
              padded_rewards,
              reward_mask,
              gamma=gamma,
              lambda_=lambda_,
              epsilon=epsilon_schedule,
              rng=k3,
          )
          logging.vlog(1, "One PPO grad desc took: %0.2f msec", get_time(t, t2))
          logging.vlog(1, "PPO loss [%10.2f] -> [%10.2f]", cur_ppo_loss,
                       new_ppo_loss)

        if early_stopping:
          break

      logging.vlog(1, "Total PPO loss reduction [%0.2f]%%",
                   (100 * (cur_ppo_loss - new_ppo_loss) / np.abs(cur_ppo_loss)))

      logging.vlog(1, "Value Optimization")

      keys2 = jax_random.split(key2, num=value_only_num_optimizer_steps)
      for j in range(value_only_num_optimizer_steps):
        k1, k2, k3 = jax_random.split(keys2[j], num=3)
        t = time.time()
        value_opt_state = value_opt_step(
            j,
            value_opt_state,
            value_opt_update,
            value_get_params,
            value_net_apply,
            padded_observations,
            padded_rewards,
            reward_mask,
            gamma=gamma,
            rng=k1)
        t2 = time.time()
        value_net_params = value_get_params(value_opt_state)
        if ((j + 1) %
            print_every_optimizer_steps == 0) or (j == num_optimizer_steps - 1):
          new_value_loss = value_loss(
              value_net_apply,
              value_net_params,
              padded_observations,
              padded_rewards,
              reward_mask,
              gamma=gamma,
              rng=k2)
          logging.vlog(1, "One value grad desc took: %0.2f msec",
                       get_time(t, t2))
          logging.vlog(1, "Value loss [%10.2f] -> [%10.2f]", cur_value_loss,
                       new_value_loss)
      logging.vlog(1, "Total value loss reduction [%0.2f]%%",
                   (100 *
                    (cur_value_loss - new_value_loss) / np.abs(cur_value_loss)))

      logging.vlog(1, "Grad desc took %0.2f msec", get_time(t1))

      logging.info(
          "Epoch [% 6d], Reward[min, max, avg] [%10.2f,%10.2f,%10.2f], "
          "ppo loss [%10.2f], value loss [%10.2f], took [%10.2f msec]", i,
          min_reward, max_reward, avg_reward, new_ppo_loss, new_value_loss,
          get_time(t0))

  if value_losses:
    logging.vlog(1, "value_losses: %s", np.stack(value_losses))
  if ppo_objective:
    logging.vlog(1, "ppo_objective:\n%s", np.stack(ppo_objective))
  if combined_losses:
    logging.vlog(1, "combined_losses:\n%s", np.stack(combined_losses))
  if average_rewards:
    logging.vlog(1, "average_rewards:\n%s", average_rewards)

  return ((policy_net_params, value_net_params), average_rewards,
          np.stack(value_losses), np.stack(ppo_objective))
