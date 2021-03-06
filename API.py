import ast
import time
from typing import Union, Dict
import requests
import pickle
import os
import asyncio
from asyncirc.protocol import IrcProtocol
from asyncirc.server import Server
from irclib.parser import Message
from database import User
import database
import contexts
import commands
from termcolor import colored
import contextlib
from more_tools import CachedProperty, BidirectionalMap
from customizable_stuff import load_command_names, load_message_templates


class API:
    def __init__(self, overlord=None, loop=None, prefix="!"):
        self.overlord = overlord
        self.twitch_client_id = 'q4nn0g7b07xfo6g1lwhp911spgutps'
        self._cache = {}
        self.streamlabs_key = ''
        self.twitch_key = ''
        self.twitch_key_requires_refreshing = False
        self.twitch_key_just_refreshed = False
        self.stream_elements_key_requires_refreshing = False
        self.stream_elements_key_just_refreshed = False
        self.stream_elements_key = ''
        self.load_keys()
        self._name = None
        self.users = []
        self.conn = None
        self.prefix = prefix
        self.commands: Dict[str, Union[commands.Command, commands.Group]] = {}
        self.loop = loop
        self.started = False
        self.console_buffer = ['placeholder']

        # self.command_names = {('acquire', None): 'buy', ('my', None): 'my', ('income', 'my'): 'income'}
        self.command_names = {}
        self.load_command_names()
        # self.command_names = {
        #     ('stocks', None): 'stocks',
        #     ('stonks', None): 'stocks',
        # }

        self.console_buffer_done = []
        self.not_sent_buffer = []

        self.streamlabs_local_send_buffer = ''
        self.streamlabs_local_receive_buffer = ''
        self.streamlabs_local_buffer_lock = asyncio.Lock()
        self.streamlabs_local_send_buffer_event = asyncio.Event()
        self.streamlabs_local_receive_buffer_event = asyncio.Event()

    def load_keys(self):
        session = database.Session()
        self.load_key(key_name='streamlabs_key', session=session)
        self.load_key(key_name='twitch_key', session=session)
        self.load_key(key_name='stream_elements_key', session=session)
        self.validate_twitch_token()

    def load_key(self, key_name, session=None):
        if session is None:
            session = database.Session()
        key_db = session.query(database.Settings).get(key_name)
        if key_db:
            setattr(self, key_name, key_db.value)
        else:
            if os.path.exists(f'lib/{key_name}'):
                with open(f'lib/{key_name}', 'rb') as f:
                    key = pickle.load(f)
                session.add(database.Settings(key=f'{key_name}', value=key))
                session.commit()
                os.remove(f'lib/{key_name}')

    def get_user(self):
        """:returns: Information regarding the Streamer through Twitch API. Currently used just for fetching the name."""
        if self.tokens_ready:
            url = "https://api.twitch.tv/helix/users"
            headers = {"Authorization": f'Bearer {self.twitch_key}', 'Client-ID': f'{self.twitch_client_id}'}
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                return res.json()
            else:
                raise ValueError("Tried fetching user info, but failed. Probably an invalid Twitch Token. Tell Razbi.")
        return ValueError("Tried fetching user info, but tokens are not ready for use. Tell Razbi. "
                          "If this actually happened, it's really really bad.")

    async def create_context(self, username, session):
        user = await self.generate_user(username, session=session)
        return contexts.UserContext(user=user, api=self, session=session)

    async def handler(self, conn, message: Union[Message, str]):
        text: str = message.parameters[1].lower()
        if not text.startswith(self.prefix):
            return
        username = message.prefix.user

        text = text[len(self.prefix):]
        old_command_name, *args = text.split()
        command_name = self.command_names.get((old_command_name, None), old_command_name)
        command_name, _, group_name = command_name.partition(" ")
        if group_name:
            args.insert(0, group_name)
        if command_name in self.commands:
            with contextlib.closing(database.Session()) as session:
                ctx = await self.create_context(username, session)
                try:
                    # noinspection PyTypeChecker
                    await self.commands[command_name](ctx, *args)
                except commands.BadArgumentCount as e:
                    self.send_chat_message(f'@{ctx.user.name} Usage: {self.prefix}{e.usage(name=old_command_name)}')
                except commands.CommandError as e:
                    self.send_chat_message(e.msg)

    @staticmethod
    async def generate_user(name, session=None):
        local_session = False
        if session is None:
            local_session = True
            session = database.Session()
        user = session.query(User).filter_by(name=name).first()
        if not user:
            user = User(name=name)
            session.add(user)
            session.commit()
        if local_session:
            session.close()
        return user

    async def start_read_chat(self):
        while not self.started:
            await asyncio.sleep(.5)
        server = [Server("irc.chat.twitch.tv", 6667, password=f'oauth:{self.twitch_key}')]
        self.conn = IrcProtocol(server, nick=self.name, loop=self.loop)
        self.conn.register('PRIVMSG', self.handler)
        await self.conn.connect()
        self.conn.send(f"JOIN #{self.name}")
        print(f"{colored('Ready to read chat commands', 'green')}. "
              f"To see all basic commands type {colored('!stocks', 'magenta')} in the twitch chat")
        self.clear_unsent_buffer()
        if self.currency_system == 'streamlabs_local':
            await self.ping_streamlabs_local()
        # self.conn.send(f"PRIVMSG #{self.name} :I'm testing if this damn thing works")
        # self.conn.send(f"PRIVMSG #{self.name} :hello")
        await asyncio.sleep(24 * 60 * 60 * 365 * 100)

    def send_chat_message(self, message: str):
        if self.conn and self.conn.connected:
            self.conn.send(f"PRIVMSG #{self.name} :{message}")
            if message != '':
                print(f"{colored('Message sent:', 'cyan')} {colored(message, 'yellow')}")
                self.console_buffer.append(str(message))
        else:
            self.not_sent_buffer.append(message)

    def clear_unsent_buffer(self):
        if self.not_sent_buffer:
            temp_buffer = self.not_sent_buffer
            self.not_sent_buffer = []
            for element in temp_buffer:
                self.send_chat_message(element)

    async def add_points(self, user: str, amount: int):
        if self.tokens_ready:
            if self.currency_system == 'streamlabs':
                url = "https://streamlabs.com/api/v1.0/points/subtract"
                querystring = {"access_token": self.streamlabs_key,
                               "username": user,
                               "channel": self.name,
                               "points": -amount}

                return requests.post(url, data=querystring).json()["points"]
            elif self.currency_system == 'stream_elements':
                url = f'https://api.streamelements.com/kappa/v2/points/{self.stream_elements_id}/{user}/{amount}'
                headers = {'Authorization': f'OAuth {self.stream_elements_key}'}
                res = requests.put(url, headers=headers)
                # print(res.json())
                if res.status_code == 200:
                    return res.json()["newAmount"]
                raise ValueError(f"Error encountered while adding points with stream_elements system. HTTP Code {res.status_code}. "
                                 "Please tell Razbi about it.")

                # return requests.put(url, headers=headers).json()["newAmount"]
            elif self.currency_system == 'streamlabs_local':
                await self.request_streamlabs_local_message(f'!add_points {user} {amount}')
                return 'it worked, I guess'

            raise ValueError(f"Unavailable Currency System: {self.currency_system}")
        raise ValueError("Tokens not ready for use. Tell Razbi about this.")

    async def upgraded_add_points(self, user: User, amount: int, session):
        if amount > 0:
            user.gain += amount
        elif amount < 0:
            user.lost -= amount
        session.commit()
        await self.add_points(user.name, amount)

    def command(self, **kwargs):
        return commands.command(registry=self.commands, **kwargs)

    def group(self, **kwargs):
        return commands.group(registry=self.commands, **kwargs)

    @property
    def name(self):
        if self._name:
            return self._name
        if self.tokens_ready:
            self._name = self.get_user()['data'][0]['login']
            return self._name

    @CachedProperty
    def currency_system(self):
        session = database.Session()
        currency_system_db = session.query(database.Settings).get('currency_system')
        if currency_system_db is not None:
            res = currency_system_db.value
        else:
            res = ''
            session.add(database.Settings(key='currency_system', value=''))
            session.commit()
        return res

    def mark_dirty(self, setting):
        if f'{setting}' in self._cache.keys():
            del self._cache[setting]

    def validate_twitch_token(self):
        if not self.twitch_key:
            return False
        url = 'https://id.twitch.tv/oauth2/validate'
        querystring = {'Authorization': f'OAuth {self.twitch_key}'}
        res = requests.get(url=url, headers=querystring)
        if res.status_code == 200:
            self.twitch_key_requires_refreshing = False
            return True
        elif res.status_code == 401:
            if not self.twitch_key_requires_refreshing:
                print("Twitch Token expired. Refreshing Token whenever possible...")
                self.twitch_key_requires_refreshing = True
            return False
        raise ValueError(
            f"A response code appeared that Razbi didn't handle when validating a twitch token, maybe tell him? Response Code: {res.status_code}")

    def validate_stream_elements_key(self):
        if not self.stream_elements_key:
            return False
        url = 'https://api.streamelements.com/oauth2/validate'
        querystring = {'Authorization': f'OAuth {self.twitch_key}'}
        res = requests.get(url=url, headers=querystring)
        if res.status_code == 200:
            self.stream_elements_key_requires_refreshing = False
            return True
        elif res.status_code == 401:
            print("Stream_elements Token expired. Refreshing Token whenever possible...")
            self.stream_elements_key_requires_refreshing = True
            return False
        elif res.status_code >= 500:
            print("server errored or... something. better tell Razbi")
            return False
        raise ValueError(
            f"A response code appeared that Razbi didn't handle when validating a stream_elements token, maybe tell him? Response Code: {res.status_code}")

    @property
    def tokens_ready(self):
        if 'tokens_ready' in self._cache:
            return self._cache['tokens_ready']
        if self.currency_system and self.twitch_key and self.validate_twitch_token():
            if self.currency_system == 'streamlabs' and self.streamlabs_key or \
                    self.currency_system == 'stream_elements' and self.stream_elements_key:
                self._cache['tokens_ready'] = True
                return True

            if self.currency_system == 'streamlabs_local':
                self._cache['tokens_ready'] = True

                # self.send_chat_message('!connect_minigame')
                # print("connected")
                return True
        return False

    @CachedProperty
    def stream_elements_id(self):
        if self.tokens_ready:
            session = database.Session()
            stream_elements_id_db = session.query(database.Settings).get('stream_elements_id')
            if stream_elements_id_db:
                return stream_elements_id_db.value
            url = f'https://api.streamelements.com/kappa/v2/channels/{self.name}'
            headers = {'accept': 'application/json'}
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                stream_elements_id = res.json()['_id']
                session.add(database.Settings(key='stream_elements_id', value=stream_elements_id))
                return stream_elements_id

    @CachedProperty
    def twitch_key_expires_at(self):
        session = database.Session()
        expires_at_db = session.query(database.Settings).get('twitch_key_expires_at')
        if expires_at_db:
            expires_at = int(expires_at_db.value)
            return expires_at

    @CachedProperty
    def stream_elements_key_expires_at(self):
        session = database.Session()
        expires_at_db = session.query(database.Settings).get('stream_elements_key_expires_at')
        if expires_at_db:
            expires_at = int(expires_at_db.value)
            return expires_at

    async def twitch_key_auto_refresher(self):
        while True:
            if self.tokens_ready and self.twitch_key_expires_at and time.time() + 60 > self.twitch_key_expires_at:
                url = 'https://razbi.funcity.org/stocks-chat-minigame/twitch/refresh_token'
                querystring = {'access_token': self.twitch_key}
                res = requests.get(url, params=querystring)
                if res.status_code == 200:
                    session = database.Session()
                    twitch_key_db = session.query(database.Settings).get('twitch_key')
                    if twitch_key_db:
                        twitch_key_db.value = res.json()['access_token']
                    expires_at_db = session.query(database.Settings).get('twitch_key_expires_at')
                    if expires_at_db:
                        expires_at_db.value = str(int(time.time()) + res.json()['expires_in'])
                    self.mark_dirty('twitch_key_expires_at')
                    session.commit()
                    print("Twitch key refreshed successfully.")
                elif res.status_code == 500:
                    print(
                        "Tried refreshing the twitch token, but the server is down or smth, please tell Razbi about this. ")
                else:
                    raise ValueError('Unhandled status code when refreshing the twitch key. TELL RAZBI',
                                     res.status_code)

            elif self.tokens_ready and self.currency_system == 'stream_elements' and \
                    self.stream_elements_key_expires_at and time.time() + 60 > self.stream_elements_key_expires_at:
                url = 'https://razbi.funcity.org/stocks-chat-minigame/stream_elements/refresh_token'
                querystring = {'access_token': self.stream_elements_key}
                res = requests.get(url, params=querystring)
                if res.status_code == 200:
                    session = database.Session()
                    stream_element_key_db = session.query(database.Settings).get('stream_elements_key')
                    if stream_element_key_db:
                        stream_element_key_db.value = res.json()['access_token']
                    expires_at_db = session.query(database.Settings).get('stream_elements_key_expires_at')
                    if expires_at_db:
                        expires_at_db.value = str(int(time.time()) + res.json()['expires_in'])
                    self.mark_dirty('stream_elements_key_expires_at')
                    session.commit()
                    print("Stream_elements key refreshed successfully.")
                elif res.status_code == 500:
                    print(
                        "Tried refreshing the stream_elements token, but the server is down or smth, please tell Razbi about this. ")
                else:
                    raise ValueError('Unhandled status code when refreshing the stream_elements key. TELL RAZBI',
                                     res.status_code)

            await asyncio.sleep(60)

    async def ping_streamlabs_local(self):
        self.send_chat_message('!connect_minigame')
        await self.request_streamlabs_local_message(f'!get_user_points {self.name}')

    async def request_streamlabs_local_message(self, message: str):
        async with self.streamlabs_local_buffer_lock:
            self.streamlabs_local_send_buffer = message
            self.streamlabs_local_send_buffer_event.set()
            await self.streamlabs_local_receive_buffer_event.wait()
            self.streamlabs_local_receive_buffer_event.clear()
            response = self.streamlabs_local_receive_buffer
        return response

    def get_and_format(self, ctx: contexts.UserContext, message_name: str, **formats):
        if message_name not in self.overlord.messages:
            default_messages = load_message_templates()
            if message_name in default_messages:
                self.overlord.messages[message_name] = default_messages[message_name]
            else:
                return ''

        return self.overlord.messages[message_name].format(stocks_alias=self.overlord.messages['stocks_alias'],
                                                           company_alias=self.overlord.messages['company_alias'],
                                                           user_name=ctx.user.name,
                                                           currency_name=self.overlord.currency_name,
                                                           stocks_limit=f'{self.overlord.max_stocks_owned:,}',
                                                           **formats)

    def load_command_names(self):
        # self.command_names = {('acquire', None): 'buy', ('my', None): 'my', ('income', 'my'): 'income'}

        session = database.Session()
        command_names = session.query(database.Settings).get('command_names')
        if command_names:
            self.command_names = BidirectionalMap(ast.literal_eval(command_names.value))
            default_command_names = load_command_names()
            for key in default_command_names:
                if key not in self.command_names or self.command_names[key] != default_command_names[key]:
                    self.command_names[key] = default_command_names[key]
        else:
            self.command_names = load_command_names()
            # command_names = database.Settings(key='command_names', value=repr(self.command_names))
            # session.add(command_names)
            # session.commit()


if __name__ == '__main__':
    api = API()

    loop = asyncio.get_event_loop()
    # api = API()
    # print(api.get_user('razbith3player'))
    # print(api.get_points('nerzul007'))
    # print(api.twitch_key)
    try:
        loop.run_until_complete(api.start_read_chat())
    finally:
        loop.stop()
    # print(type(api.twitch_key))
    # api.read_chat()
    # loop.run_until_complete(api.read_chat())
    # just FYI, if you plan on testing just API alone, do it in the Overlord.py, not here
