from collections import deque

import gym
import ray

from phillip import ssbm
from phillip.env.ssbm_env import SSBMEnv
from phillip.rllib import ssbm_spaces


class MultiSSBMEnv(ray.rllib.env.MultiAgentEnv):

  def __init__(self, config):
    print("MultiSSBMEnv", config.keys())
    self._ssbm_config = config["ssbm_config"]
    self._episode_length = config["episode_length"]
    self._steps_this_episode = 0
    self._env = None
    self._act_every = config.get("act_every", 3)
    action_set = ssbm.actionTypes["custom_sh2_wd"]
    self._action_chains = action_set.get_action_chains(self._act_every)
    
    #self.action_space = None
    self.action_space = gym.spaces.Discrete(action_set.size)
    #self.observation_space = None
    self.observation_space = ssbm_spaces.game_conv_list[0].space

  def _get_obs(self):
    game_state = self._env.get_state()
    return {
      pid: ssbm_spaces.game_conv_list[pid](game_state)
      for pid in self._env.ai_pids
    }

  def reset(self):
    if self._env is None:
      self._env = SSBMEnv(**self._ssbm_config)
    return self._get_obs()
  
  def step(self, actions):
    self._steps_this_episode += 1
    chains = {pid: self._action_chains[action] for pid, action in actions.items()}
    
    for i in range(self._act_every):
      self._env.step({
          pid: chain[i].get_real_controller(self._env.characters[pid])
          for pid, chain in chains.items()
       })
    
    obs = self._get_obs()
    rewards = {i: 0 for i in self._env.ai_pids}

    done = self._steps_this_episode == self._episode_length
    if done:
      print("episode terminated")
      self._steps_this_episode = 0
    else:
      print("episode step %d", self._steps_this_episode)
    #dones = {i: done for i in self._env.ai_pids}
    #dones.update(__all__=done)
    dones = {"__all__": done}
    return obs, rewards, dones, {}

#RemoteSSBMEnv = ray.remote(MultiSSBMEnv)

@ray.remote
class RemoteSSBMEnv:
  def __init__(self, config):
    print("RemoteSSBMEnv", config.keys())
    self._env = MultiSSBMEnv(config)
  
  def reset(self):
    return self._env.reset()
  
  def step(self, action_dict):
    return self._env.step(action_dict)


def seq_to_dict(seq):
  return {i: x for i, x in enumerate(seq)}

def map_dict(f, d):
  return {k: f(v) for k, v in d.items()}

NONE_DONE = {"__all__": False}


class AsyncSSBMEnv(ray.rllib.env.BaseEnv):
  
  def __init__(self, config):
    print("AsyncSSBMEnv", config.keys())
    self._config = config
    self._dummy_env = MultiSSBMEnv(config)
    self.action_space = self._dummy_env.action_space
    self.observation_space = self._dummy_env.observation_space
    self._first_poll = True

    self._num_envs = config["num_envs"]
    self._delay = config["delay"]
    self._queue = deque()
  
  def first_poll(self):
    print("first poll")
    print("AsyncSSBMEnv", self._config.keys())
    self._envs = [
      RemoteSSBMEnv.remote(self._config)
      for _ in range(self._num_envs)]

    obs = ray.get([env.reset.remote() for env in self._envs])
    rewards = [map_dict(lambda _: 0., ob) for ob in obs]
    dones = [NONE_DONE for ob in obs]
    infos = [map_dict(lambda _: {}, ob) for ob in obs]
    
    dummy_actions = [map_dict(lambda _: 0, ob) for ob in obs]
    for _ in range(self._delay):
      self.send_actions(dummy_actions)
    
    self._first_poll = False
    return tuple(map(seq_to_dict, (obs, rewards, dones, infos))) + ({},)
    
  def poll(self):
    if self._first_poll:
      return self.first_poll()
    fetched = ray.get(self._queue.popleft())
    return tuple(map(seq_to_dict, zip(*fetched))) + ({},)

  def send_actions(self, action_dict):
    self._queue.append([
        env.step.remote(action_dict[i])
        for i, env in enumerate(self._envs)
    ])

