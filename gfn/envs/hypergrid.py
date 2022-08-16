"""
Copied and Adapted from https://github.com/Tikquuss/GflowNets_Tutorial
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Tuple

import torch
from torchtyping import TensorType

from gfn.containers import States, make_States_class
from gfn.envs.env import AbstractStatesBatch, Env

# Typing
TensorLong = TensorType["batch_shape", torch.long]
TensorFloat = TensorType["batch_shape", torch.float]
TensorBool = TensorType["batch_shape", torch.bool]
ForwardMasksTensor = TensorType["batch_shape", "n_actions", torch.bool]
BackwardMasksTensor = TensorType["batch_shape", "n_actions - 1", torch.bool]
OneStateTensor = TensorType["state_shape", torch.float]
StatesTensor = TensorType["batch_shape", "state_shape", torch.float]


class HyperGrid2(Env):
    def __init__(
        self,
        ndim: int = 2,
        height: int = 8,
        R0: float = 0.1,
        R1: float = 0.5,
        R2: float = 2.0,
        reward_cos: bool = False,
    ):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        s_0 = torch.zeros(ndim, device=device)
        s_f = torch.ones(ndim, device=device) * (-1)
        n_actions = ndim + 1
        super().__init__(n_actions, s_0, s_f)
        self.ndim = ndim
        self.height = height
        self.R0 = R0
        self.R1 = R1
        self.R2 = R2
        self.reward_cos = reward_cos

    def make_random_states(self, batch_shape: Tuple[int]) -> StatesTensor:
        return torch.randint(0, self.height, batch_shape)

    def update_masks(self, states: States) -> None:
        # TODO: probably not the best way to do this
        states.forward_masks[..., :-1] = states.states != self.height - 1
        states.backward_masks = states.states != 0

    def step(
        self,
        states: States,
        actions: TensorLong,
    ) -> States:

        new_states = deepcopy(states)

        sink_states = new_states.is_sink_state()
        non_sink_states_masks = new_states.forward_masks[~sink_states]
        non_sink_actions = actions[~sink_states]
        actions_valid = all(
            torch.gather(non_sink_states_masks, 1, non_sink_actions.unsqueeze(1))
        )
        if not actions_valid:
            raise ValueError("Actions are not valid")

        new_sink_states = sink_states | self.is_exit_actions(actions)
        new_states.is_sink = new_sink_states

        not_done_states = new_states.states[~new_sink_states]
        not_done_actions = actions[~new_sink_states]

        not_done_states.scatter_(-1, not_done_actions.unsqueeze(-1), 1, reduce="add")

        new_states.states[~new_sink_states] = not_done_states

        self.update_masks(new_states)

        return new_states

    def backward_step(self, states: States, actions: TensorLong) -> States:

        new_states = deepcopy(states)
        initial_states = new_states.is_initial_state()
        non_initial_states_masks = new_states.backward_masks[~initial_states]
        non_initial_actions = actions[~initial_states]
        actions_valid = all(
            torch.gather(non_initial_states_masks, 1, non_initial_actions.unsqueeze(1))
        )
        if not actions_valid:
            raise ValueError("Actions are not valid")

        not_done_states = new_states.states[~initial_states]
        not_done_states.scatter_(
            -1, non_initial_actions.unsqueeze(-1), -1, reduce="add"
        )

        new_states.states[~initial_states] = not_done_states

        new_initial_states = initial_states | (new_states.states.sum(-1) == 0)

        new_states.is_initial = new_initial_states

        self.update_masks(new_states)

        return new_states

    def reward(self, final_states: States) -> TensorFloat:
        final_states_raw = final_states.states
        R0, R1, R2 = (self.R0, self.R1, self.R2)
        ax = abs(final_states_raw / (self.height - 1) - 0.5)
        if not self.reward_cos:
            reward = (
                R0 + (0.25 < ax).prod(-1) * R1 + ((0.3 < ax) * (ax < 0.4)).prod(-1) * R2
            )
        else:
            pdf_input = ax * 5
            pdf = 1.0 / (2 * torch.pi) ** 0.5 * torch.exp(-(pdf_input**2) / 2)
            reward = R0 + ((torch.cos(ax * 50) + 1) * pdf).prod(-1) * R1
        return reward

    def get_states_indices(self, states):
        states_raw = states.states
        canonical_base = self.height ** torch.arange(self.ndim - 1, -1, -1)
        flat_indices = (canonical_base * states_raw).sum(-1).long()
        return flat_indices


@dataclass
class HyperGrid(Env):
    "Hypergrid environment"
    ndim: int = 2
    height: int = 8
    R0: float = 1e-1
    R1: float = 0.5
    R2: float = 2.0
    reward_cos: bool = False

    def __post_init__(self):
        self.state_shape = (self.ndim,)
        self.n_actions = self.ndim + 1
        super().__post_init__()
        self.n_states = self.height**self.ndim

    def make_state_class(self):
        envSelf = self

        @dataclass
        class StatesBatch(AbstractStatesBatch):
            batch_shape: Tuple[int, ...] = (envSelf.n_envs,)
            state_dim = (envSelf.ndim,)
            shape: Tuple[int, ...] = field(init=False)
            states: TensorType[(*batch_shape, *state_dim)] = None
            masks: TensorType[(*batch_shape, envSelf.n_actions)] = field(init=False)
            backward_masks: TensorType[(*batch_shape, envSelf.n_actions - 1)] = field(
                init=False
            )
            already_dones: TensorType[batch_shape] = field(init=False)

            def __post_init__(self):
                if self.states is None and not self.random:
                    self.shape = (*self.batch_shape, *self.state_dim)
                    self.states = torch.zeros(
                        self.shape, dtype=torch.long, device=envSelf.device
                    )
                elif self.random:
                    self.shape = (*self.batch_shape, *self.state_dim)
                    self.states = torch.randint(
                        0, envSelf.height, self.shape, device=envSelf.device
                    )
                else:
                    assert self.states.shape[-1] == envSelf.ndim
                    self.batch_shape = tuple(self.states.shape[:-1])
                    self.shape = (*self.batch_shape, *self.state_dim)
                # self.masks = self.make_masks()
                # self.backward_masks = self.make_backward_masks()
                super().__post_init__()

            def __repr__(self):
                return f"StatesBatch(\nstates={self.states},\n masks={self.masks},\n backward_masks={self.backward_masks},\n already_dones={self.already_dones})"

            def make_masks(self) -> TensorType[..., envSelf.n_actions, bool]:
                states = self.states
                batch_shape = tuple(states.shape[:-1])
                masks = torch.ones(
                    (*batch_shape, envSelf.n_actions),
                    dtype=torch.bool,
                    device=envSelf.device,
                )
                masks[
                    torch.cat(
                        [
                            states == envSelf.height - 1,
                            torch.zeros(
                                (*batch_shape, 1),
                                dtype=torch.bool,
                                device=envSelf.device,
                            ),
                        ],
                        -1,
                    )
                ] = False
                return masks

            def make_backward_masks(
                self,
            ) -> TensorType[..., envSelf.n_actions - 1, bool]:
                states = self.states
                batch_shape = tuple(states.shape[:-1])
                masks = torch.ones(
                    (*batch_shape, envSelf.n_actions - 1),
                    dtype=torch.bool,
                    device=envSelf.device,
                )

                masks[states == 0] = False
                return masks

            def update_the_dones(self):
                states = self.states
                self.already_dones = states.sum(-1) == 0

        return StatesBatch

    def step(self, actions):
        not_done_states_masks = self._state.masks[~self._state.already_dones]
        not_done_actions = actions[~self._state.already_dones]
        actions_valid = all(
            torch.gather(not_done_states_masks, 1, not_done_actions.unsqueeze(1))
        )
        if not actions_valid:
            raise ValueError("Actions are not valid")

        dones = self._state.already_dones | (actions == self.n_actions - 1)
        self._state.already_dones = dones

        not_done_states = self._state.states[~dones]
        not_done_actions = actions[~dones]

        n_states_to_update = len(not_done_actions)
        not_done_states[torch.arange(n_states_to_update), not_done_actions] += 1

        self._state.states[~dones] = not_done_states

        self._state.update_masks()

        return deepcopy(self._state), dones

    def backward_step(self, states, actions):
        states = deepcopy(states)
        not_done_states_masks = states.backward_masks[~states.already_dones]
        not_done_actions = actions[~states.already_dones]
        actions_valid = all(
            torch.gather(not_done_states_masks, 1, not_done_actions.unsqueeze(1))
        )
        if not actions_valid:
            raise ValueError("Actions are not valid")

        not_done_states = states.states[~states.already_dones]
        n_states_to_update = len(not_done_actions)
        not_done_states[torch.arange(n_states_to_update), not_done_actions] -= 1

        states.states[~states.already_dones] = not_done_states

        dones = states.already_dones | (states.states.sum(-1) == 0)

        states.already_dones = dones

        states.update_masks()

        return states, dones

    def reward(self, final_states):
        if isinstance(final_states, AbstractStatesBatch):
            final_states = final_states.states
        R0, R1, R2 = (self.R0, self.R1, self.R2)
        ax = abs(final_states / (self.height - 1) - 0.5)
        if not self.reward_cos:
            reward = (
                R0 + (0.25 < ax).prod(-1) * R1 + ((0.3 < ax) * (ax < 0.4)).prod(-1) * R2
            )
        else:
            pdf_input = ax * 5
            pdf = 1.0 / (2 * torch.pi) ** 0.5 * torch.exp(-(pdf_input**2) / 2)
            reward = R0 + ((torch.cos(ax * 50) + 1) * pdf).prod(-1) * R1
        return reward

    def get_states_indices(self, states):
        if isinstance(states, AbstractStatesBatch):
            states = states.states
        canonical_base = self.height ** torch.arange(self.ndim - 1, -1, -1)
        flat_indices = (canonical_base * states).sum(-1).long()
        return flat_indices


if __name__ == "__main__":
    print("Testing HyperGrid env with 3 environments, and height of 3")
    env = HyperGrid(n_envs=3, height=3)
    env.reset()
    actions = torch.tensor([0, 1, 1], dtype=torch.long)
    states, dones = env.step(actions)
    actions = torch.tensor([0, 2, 1], dtype=torch.long)
    states, dones = env.step(actions)
    actions = torch.tensor([1, 1, 2], dtype=torch.long)
    states, dones = env.step(actions)
    try:
        actions = torch.tensor([0, 1, 1], dtype=torch.long)
        states, dones = env.step(actions)
    except ValueError:
        print("ValueError raised as expected because of invalid actions")
    print(env._state)
    print("Final rewards:", env.reward(env._state))

    states.zero_the_dones()
    print("States after zeroing dones:", states)

    try:
        actions = torch.tensor([1, 1, 2], dtype=torch.long)
        states, dones = env.backward_step(states, actions)
    except RuntimeError:
        print("RuntimeError raised as expected because of invalid actions")

    actions = torch.tensor([1, 1, 1], dtype=torch.long)
    states, dones = env.backward_step(states, actions)

    # second is already done, so 43 is ok
    actions = torch.tensor([0, 43, 1], dtype=torch.long)
    states, dones = env.backward_step(states, actions)

    # second is already done, so 43 is ok
    actions = torch.tensor([0, 43, 34], dtype=torch.long)
    states, dones = env.backward_step(states, actions)
    if all(dones):
        print("Initial states reached everywhere,", states)
    else:
        raise ValueError("Initial states not reached everywhere")

    print("Testing state creating with given states:")
    states = torch.randint(0, env.height, (5, 3, env.ndim))
    states_batch = env.StatesBatch(states=states)
    print("Testing done updating. For states that are s_0, done should be True:")
    states_batch.update_the_dones()
    print(states_batch)
