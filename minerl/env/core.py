# ------------------------------------------------------------------------------------------------
# Copyright (c) 2018 Microsoft Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# ------------------------------------------------------------------------------------------------

import collections

import copy
import json
import logging
import os
import random
import socket
import struct
import time
import uuid
from copy import copy, deepcopy
from typing import Iterable
from functools import partial

import gym
import gym.envs.registration
import gym.spaces
import minerl.env.spaces
import numpy as np
from lxml import etree
from minerl.env import comms, malmo
from minerl.env.comms import retry
from minerl.env.malmo import InstanceManager, malmo_version, launch_queue_logger_thread
from minerl.env.observations import pov_observation, inventory_observation, compass_observation

logger = logging.getLogger(__name__)

missions_dir = os.path.join(os.path.dirname(__file__), 'missions')


class EnvException(Exception):
    """A special exception thrown in the creation of an environment's Malmo mission XML.

    Args:
        message (str): The exception message.
    """

    def __init__(self, message):
        super(EnvException, self).__init__(message)


class MissionInitException(Exception):
    """An exception thrown when a mission fails to initialize

    Args:
        message (str): The exception message.
    """

    def __init__(self, message):
        super(MissionInitException, self).__init__(message)


MAX_WAIT = 80  # After this many MALMO_BUSY's a timeout exception will be thrown
SOCKTIME = 60.0 * 4  # After this much time a socket exception will be thrown.
MINERL_CUSTOM_ENV_ID = 'MineRLCustomEnv' # Default id for a MineRLEnv


class MineRLEnv(gym.Env):
    """The MineRLEnv class.

        Example:
            To actually create a MineRLEnv. Use any one of the package MineRL environments (Todo: Link.)
            literal blocks::

                import minerl
                import gym

                env = gym.make('MineRLTreechop-v0') # Makes a minerl environment.

                # Use env like any other OpenAI gym environment.
                # ...


        Args:
            xml (str): The path to the MissionXML file for this environment.
            observation_space (gym.Space): The observation for the environment.
            action_space (gym.Space): The action space for the environment.
            port (int, optional): The port of an exisitng Malmo environment. Defaults to None.
        """
    metadata = {'render.modes': ['rgb_array', 'human']}

    STEP_OPTIONS = 0
    DEFAULT_OBS_HANDLERS = {
        'pov': pov_observation,
        'inventory': inventory_observation,
        'compassAngle': compass_observation,
    }

    def __init__(self, xml, observation_space, action_space,
                 docstr=None, obs_handlers=None):
        self.action_space = action_space
        self.observation_space = observation_space
        self.obs_handlers = deepcopy(self.DEFAULT_OBS_HANDLERS)
        if obs_handlers is not None:
            self.obs_handlers.update(obs_handlers)

        self.viewer = None

        self.xml = None
        self.integratedServerPort = 0
        self.role = 0
        self.agent_count = 0
        self.resets = 0
        self.ns = '{http://ProjectMalmo.microsoft.com}'
        self.client_socket = None

        self.exp_uid = ""
        self.done = True
        self.synchronous = True

        self.width = 0
        self.height = 0
        self.depth = 0

        self.xml_file = xml
        self.has_init = False
        self._seed = None
        self.had_to_clean = False

        self._already_closed = False
        self.instance = self._robust_launch_new_instance()

        self.resets = 0
        self.done = True

    def _attempt_launch_new_instance(self):
        """Returns a successfully launched Instance, or None if the launch had
        an intermittent build error."""
        instance = InstanceManager.get_instance(os.getpid())
        if InstanceManager.is_remote():
            launch_queue_logger_thread(instance, self.is_closed)

        try:
            instance.launch()
        except malmo.IntermittentBuildError:
            instance.kill()
            instance = None
        return instance

    def _robust_launch_new_instance(self, *, max_tries=3) -> InstanceManager:
        """Launch and return a new InstanceManager. Attempt up to `max_tries` times."""
        for i in range(max_tries):
            instance = self._attempt_launch_new_instance()
            if instance is not None:
                return instance
            else:
                logger.warning(f"Minecraft build or launch just failed on attempt {i}. "
                               "This is probably an intermittent race condition. ",
                               f"Trying again (max tries {max_tries}).")
                time.sleep(3)
        raise RuntimeError(f"Failed to build and launch Minecraft instance "
                           f"{max_tries} times. Giving up.")

    def init(self):
        """Initializes the MineRL Environment.

        Note:
            This is called automatically when the environment is made.

        Raises:
            EnvException: If the Mission XML is malformed this is thrown.
            ValueError: The space specified for this environment does not have a default action.
            NotImplementedError: When multiagent environments are attempted to be used.
        """
        exp_uid = None

        # Parse XML file
        with open(self.xml_file, 'r') as f:
            xml = f.read()
        # Todo: This will fail when using a remote instance manager.

        xml = xml.replace('$(MISSIONS_DIR)', missions_dir)

        if self.spec is not None:
            xml = xml.replace('$(ENV_NAME)', self.spec.id)
        else:
            xml = xml.replace('$(ENV_NAME)', MINERL_CUSTOM_ENV_ID)

        # Bootstrap the environment if it hasn't been.
        role = 0

        if not xml.startswith('<Mission'):
            i = xml.index("<Mission")
            if i == -1:
                raise EnvException("Mission xml must contain <Mission> tag.")
            xml = xml[i:]

        self.xml = etree.fromstring(xml)
        self.role = role
        if exp_uid is None:
            self.exp_uid = str(uuid.uuid4())
        else:
            self.exp_uid = exp_uid

        # Force single agent
        self.agent_count = 1
        turn_based = self.xml.find(
            './/' + self.ns + 'TurnBasedCommands') is not None
        if turn_based:
            raise NotImplementedError(
                "Turn based or multi-agent environments not supported.")

        e = etree.fromstring("""<MissionInit xmlns="http://ProjectMalmo.microsoft.com"
                                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                                SchemaVersion="" PlatformVersion=""" + '\"' + malmo_version + '\"' +
                             """>
                                <ExperimentUID></ExperimentUID>
                                <ClientRole>0</ClientRole>
                                <ClientAgentConnection>
                                    <ClientIPAddress>127.0.0.1</ClientIPAddress>
                                    <ClientMissionControlPort>0</ClientMissionControlPort>
                                    <ClientCommandsPort>0</ClientCommandsPort>
                                    <AgentIPAddress>127.0.0.1</AgentIPAddress>
                                    <AgentMissionControlPort>0</AgentMissionControlPort>
                                    <AgentVideoPort>0</AgentVideoPort>
                                    <AgentDepthPort>0</AgentDepthPort>
                                    <AgentLuminancePort>0</AgentLuminancePort>
                                    <AgentObservationsPort>0</AgentObservationsPort>
                                    <AgentRewardsPort>0</AgentRewardsPort>
                                    <AgentColourMapPort>0</AgentColourMapPort>
                                    </ClientAgentConnection>
                                </MissionInit>""")
        e.insert(0, self.xml)
        self.xml = e
        self.xml.find(self.ns + 'ClientRole').text = str(self.role)
        self.xml.find(self.ns + 'ExperimentUID').text = self.exp_uid
        file_world_generator = self.xml.find('.//' + self.ns + 'FileWorldGenerator')
        if file_world_generator is not None:
            fileworld_path = file_world_generator.attrib['src']
            if not os.path.isabs(fileworld_path):
                # If the path for the FileWorldGenerator is a relative path,
                # assume it to be relative to the xml file itself
                xml_directory = os.path.dirname(self.xml_file)
                new_fileworld_path = os.path.join(xml_directory, fileworld_path)
                self.xml.find('.//' + self.ns + 'FileWorldGenerator').attrib['src'] = new_fileworld_path
        if self.role != 0 and self.agent_count > 1:
            e = etree.Element(self.ns + 'MinecraftServerConnection',
                              attrib={'address': self.instance.host,
                                      'port': str(0)
                                      })
            self.xml.insert(2, e)

        video_producers = self.xml.findall('.//' + self.ns + 'VideoProducer')
        assert len(video_producers) == self.agent_count
        video_producer = video_producers[self.role]
        # Todo: Deprecate width, height, and POV forcing.
        self.width = int(video_producer.find(self.ns + 'Width').text)
        self.height = int(video_producer.find(self.ns + 'Height').text)
        want_depth = video_producer.attrib["want_depth"]
        self.depth = 4 if want_depth is not None and (
            want_depth == "true" or want_depth == "1" or want_depth is True) else 3
        # print(etree.tostring(self.xml))

        self.has_init = True

    def _process_observation(self, pov, info):
        """
        Process observation into the proper dict space.
        """
        pov = np.frombuffer(pov, dtype=np.uint8)

        if pov is None or len(pov) == 0:
            pov = np.zeros(
                (self.height, self.width, self.depth), dtype=np.uint8)
        else:
            pov = pov.reshape((self.height, self.width, self.depth))[
                ::-1, :, :]

        if info:
            info = json.loads(info)
        else:
            info = {}
        info['pov'] = self._last_pov = pov

        obs_space = deepcopy(self.observation_space.spaces)
        obs_dict = {}
        try:
            for key in obs_space:
                handler_fn = self.obs_handlers[key]
                obs_dict[key] = handler_fn(info, obs_space)
        except KeyError:
            print("Could not find handler for observation space {}, returning empty.".format(key))
            return {}

        return obs_dict

    def _process_action(self, action_in) -> str:
        """
        Process the actions into a proper command.
        """
        action_in = deepcopy(action_in)
        action_str = []
        for act in action_in:
            # Process enums.
            if isinstance(self.action_space.spaces[act], minerl.env.spaces.Enum):
                if np.issubdtype(type(action_in[act]), np.integer):
                    action_in[act] = self.action_space.spaces[act].values[action_in[act]]
                else:
                    assert isinstance(
                        action_in[act], str), "Enum action {} must be str or int. Value observed: {} ".format(act, action_in[act])
                    assert action_in[act] in self.action_space.spaces[act].values, \
                        "Invalid string value for enum action {}, {}".format(act, action_in[act])

            elif isinstance(self.action_space.spaces[act], gym.spaces.Box):
                subact = action_in[act]
                assert not isinstance(
                    subact, str), "Box action {} is a string! It should be a ndarray: {}".format(act, subact)
                if isinstance(subact, np.ndarray):
                    subact = subact.flatten()

                if isinstance(subact, Iterable):
                    subact = " ".join(str(x) for x in subact)

                action_in[act] = subact

            action_str.append(
                "{} {}".format(act, str(action_in[act])))

        return "\n".join(action_str)

    @staticmethod
    def _hello(sock):
        comms.send_message(sock, ("<MalmoEnv" + malmo_version + "/>").encode())

    def reset(self):
        # Add support for existing instances.
        try:
            if not self.has_init:
                self.init()

            while not self.done:
                self.done = self._quit_episode()

                if not self.done:
                    time.sleep(0.1)

            return self._start_up()
        finally:
            # We don't force the same seed every episode, you gotta send it yourself queen.
            self._seed = None

    @retry
    def _start_up(self):
        self.resets += 1

        try:
            if not self.client_socket:

                logger.debug("Creating socket connection!")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(SOCKTIME)
                sock.connect((self.instance.host, self.instance.port))
                self._hello(sock)

                # Now retries will use connected socket.
                self.client_socket = sock
            self._init_mission()

            self.done = False
            return self._peek_obs()
        except (socket.timeout, socket.error) as e:
            self.log_error("Failed to reset (socket error), trying again!")
            self._clean_connection()
            raise e
        except RuntimeError as e:
            # Reset the instance if there was an error timeout waiting for episode pause.
            self.had_to_clean = True
            self._clean_connection()
            raise e

    def _clean_connection(self):
        self.log_error("Cleaning connection! Something must have gone wrong.")
        try:
            if self.client_socket:
                self.client_socket.shutdown(socket.SHUT_RDWR)
                self.client_socket.close()
        except (BrokenPipeError, OSError, socket.error):
            # There is no connection left!
            pass

        self.client_socket = None
        if self.had_to_clean:
            # Connect to a new instance!!
            self.log_error(
                "Connection with Minecraft client cleaned more than once; restarting.")
            if self.instance:
                self.instance.kill()
            self.instance = self._robust_launch_new_instance()
                
            self.had_to_clean = False
        else:
            self.had_to_clean = True

    def _peek_obs(self):
        obs = None
        info = None
        start_time = time.time()
        if not self.done:
            logger.debug("Peeking the client.")
            peek_message = "<Peek/>"
            comms.send_message(self.client_socket, peek_message.encode())
            obs = comms.recv_message(self.client_socket)
            info = comms.recv_message(self.client_socket).decode('utf-8')

            reply = comms.recv_message(self.client_socket)
            done, = struct.unpack('!b', reply)
            self.done = done == 1
            if obs is None or len(obs) == 0:
                if time.time() - start_time > MAX_WAIT:
                    self.client_socket.close()
                    self.client_socket = None
                    raise MissionInitException(
                        'too long waiting for first observation')
                time.sleep(0.1)
            if self.done:
                raise RuntimeError(
                    "Something went wrong resetting the environment! "
                    "`done` was true on first frame.")

        return self._process_observation(obs, info)

    def _quit_episode(self):
        comms.send_message(self.client_socket, "<Quit/>".encode())
        reply = comms.recv_message(self.client_socket)
        ok, = struct.unpack('!I', reply)
        return ok != 0

    def seed(self, seed=None):
        """Seeds the environment!

        Note:
        THIS MUST BE CALLED BEFORE :code:`env.reset()`
        
        Args:
            seed (long, optional):  Defaults to None.
        """
        assert isinstance(seed, int), "Seed must be an int!"
        self._seed = seed

    def step(self, action):

        withinfo = MineRLEnv.STEP_OPTIONS == 0 or MineRLEnv.STEP_OPTIONS == 2

        # Process the actions.
        malmo_command = self._process_action(action)
        try:
            if not self.done:

                step_message = "<Step" + str(MineRLEnv.STEP_OPTIONS) + ">" + \
                    malmo_command + \
                    "</Step" + str(MineRLEnv.STEP_OPTIONS) + " >"

                # Send Actions.
                comms.send_message(self.client_socket, step_message.encode())

                # Receive the observation.
                obs = comms.recv_message(self.client_socket)

                # Receive reward done and sent.
                reply = comms.recv_message(self.client_socket)
                reward, done, sent = struct.unpack('!dbb', reply)

                # Receive info from the environment.
                if withinfo:
                    info = comms.recv_message(
                        self.client_socket).decode('utf-8')
                else:
                    info = {}

                # Process the observation and done state.
                out_obs = self._process_observation(obs, info)
                self.done = (done == 1)

                return out_obs, reward, self.done, {}
            else:
                raise RuntimeError(
                    "Attempted to step an environment with done=True")
        except (socket.timeout, socket.error) as e:
            # If the socket times out some how! We need to catch this and reset the environment.
            self._clean_connection()
            self.done = True
            self.log_error(
                "Failed to take step (timeout or error). Terminating episode and sending random observation, be aware. "
                "To account for this failure case in your code check to see if `'error' in info` where info is "
                "the info dictionary returned by the step function.")
            return self.observation_space.sample(), 0, self.done, {"error": "Connection timed out!"}

    def _renderObs(self, obs):
        if self.viewer is None:
            from gym.envs.classic_control import rendering
            import pyglet

            class ScaledWindowImageViewer(rendering.SimpleImageViewer):
                def __init__(self, width, height):
                    super().__init__(None, 640)

                    if width > self.maxwidth:
                        scale = self.maxwidth / width
                        width = int(scale * width)
                        height = int(scale * height)
                    self.window = pyglet.window.Window(width=width, height=height, 
                        display=self.display, vsync=False, resizable=True)            
                    self.width = width
                    self.height = height
                    self.isopen = True

                    @self.window.event
                    def on_resize(width, height):
                        self.width = width
                        self.height = height

                    @self.window.event
                    def on_close():
                        self.isopen = False

            self.viewer = ScaledWindowImageViewer(self.width*4, self.height*4)
        self.viewer.imshow(obs)
        return self.viewer.isopen

    def render(self, mode='human'):
        if mode == 'human' and ('AICROWD_IS_GRADING' not in os.environ or os.environ['AICROWD_IS_GRADING'] is None):
            self._renderObs(self._last_pov)
        return self._last_pov

    def is_closed(self):
        return self._already_closed

    def close(self):
        """gym api close"""
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None

        if self._already_closed:
            return

        if self.client_socket:
            self.client_socket.close()
            self.client_socket = None

        if self.instance and self.instance.running:
            self.instance.kill()

        self._already_closed = True

    def reinit(self):
        """Use carefully to reset the episode count to 0."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.instance.host, self.instance.port))
        self._hello(sock)

        comms.send_message(
            sock, ("<Init>" + self._get_token() + "</Init>").encode())
        reply = comms.recv_message(sock)
        sock.close()
        ok, = struct.unpack('!I', reply)
        return ok != 0

    def status(self):
        """Get status from server.
        head - Ping the the head node if True.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.instance.host, self.instance.port))

        self._hello(sock)

        comms.send_message(sock, "<Status/>".encode())
        status = comms.recv_message(sock).decode('utf-8')
        sock.close()
        return status

    def _find_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.instance.host, self.instance.port))
        self._hello(sock)

        start_time = time.time()
        port = 0
        while port == 0:
            comms.send_message(
                sock, ("<Find>" + self._get_token() + "</Find>").encode())
            reply = comms.recv_message(sock)
            port, = struct.unpack('!I', reply)
            if port == 0:
                if time.time() - start_time > MAX_WAIT:
                    if self.client_socket:
                        self.client_socket.close()
                        self.client_socket = None
                    raise MissionInitException(
                        'too long finding mission to join')
                time.sleep(1)
        sock.close()
        # print("Found mission integrated server port " + str(port))
        self.integratedServerPort = port
        e = self.xml.find(self.ns + 'MinecraftServerConnection')
        if e is not None:
            e.attrib['port'] = str(self.integratedServerPort)

    def _init_mission(self):
        ok = 0
        num_retries = 0
        logger.debug("Sending mission init!")
        while ok != 1:
            xml = etree.tostring(self.xml)
            token = (self._get_token() + ":" + str(self.agent_count) +
                     ":" + str(self.synchronous).lower())
            if self._seed is not None:
                token += ":{}".format(self._seed)
            token = token.encode()
            comms.send_message(self.client_socket, xml)
            comms.send_message(self.client_socket, token)

            reply = comms.recv_message(self.client_socket)
            ok, = struct.unpack('!I', reply)
            if ok != 1:
                num_retries += 1
                if num_retries > MAX_WAIT:
                    raise socket.timeout()
                elif self.log_shows_bind_exception():
                    # Skips error handler to directly to abort.
                    self.had_to_clean = True
                    logger.error("Malmo server failed to bind to port, possibly due "
                                 "to collision with parallel Malmo instance. "
                                 "Giving up on contacting this Malmo server, and "
                                 "starting a new one. It's possible that the abandoned Malmo "
                                 "server cannot be closed automatically, and you will "
                                 "have to do so manually later.")
                    raise RuntimeError("Port is unusable")
                else:
                    self.log_error("Did not get an OK from Malmo; trying again.")
                    time.sleep(1)

    def _get_token(self):
        return self.exp_uid + ":" + str(self.role) + ":" + str(self.resets)

    def log_error(self, msg, num_lines=5):
        lines = self._get_logs(num_lines=num_lines)
        logger.error(msg)
        logger.error('Last {} lines of the log file:'.format(len(lines)))
        for line in lines:
            logger.error(line)

    def print_logs(self, num_lines=5):
        lines = self._get_logs(num_lines=num_lines)
        print('Last {} lines of the log file:'.format(len(lines)))
        for line in lines:
            print(line)

    def log_shows_bind_exception(self, num_lines=10, verbose=True):
        error_msg = 'java.net.BindException'
        for line in self._get_logs(num_lines):
            if error_msg in line:
                if verbose:
                    print(f"Detected BindException (!): {line}")
                return True
        return False

    def _get_logs(self, num_lines=5):
        if not (self.instance and self.instance.minecraft_dir):
            print('Warning: Cannot print logs, as there is no launched instance')
            return

        log_file = os.path.join(self.instance.minecraft_dir, 'run', 'logs', 'latest.log')
        print(log_file)
        return tail(log_file, lines=num_lines)


def make():
    return Env()


def register(id, **kwargs):
    # TODO create doc string based on registered envs
    return gym.envs.register(id, **kwargs)


def _bind(instance, func, as_name=None):
    """
    Bind the function *func* to *instance*, with either provided name *as_name*
    or the existing name of *func*. The provided *func* should accept the
    instance as the first argument, i.e. "self".
    """
    if as_name is None:
        as_name = func.__name__
    bound_method = func.__get__(instance, instance.__class__)
    setattr(instance, as_name, bound_method)
    return bound_method


def tail(filename, lines=1, buffer=4098):
    """Tail a file and get X lines from the end"""
    with open(filename, "r") as f:
        lines_found = []
        block_counter = 1
        while len(lines_found) < lines:
            try:
                f.seek(-block_counter * buffer, os.SEEK_END)
            except IOError:  # either file is too small, or too many lines requested
                f.seek(0)
                lines_found = f.readlines()
                break

            lines_found = f.readlines()
            # Exponential search: get twice as many blocks next iteration
            block_counter *= 2

    return lines_found[-lines:]
