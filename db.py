import sqlite3
import json
import discord
from discord.ext import commands
from dataclasses import asdict, dataclass
import datetime

def json_datetime_serializer(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} is not serializable")

@dataclass
class RoleRequest:
    requester_id: int
    day: int = None
    time: str = None
    ping_notice: int = None
    location: str = None
    current_ep: int = 1
    total_eps: int = None
    ep_rate: int = 1
    emoji: str = None

@dataclass
class RoleClass:
    role_id: int
    day: int = None
    time: str = None
    ping_notice: int = None
    current_ep: int = None
    total_eps: int = None
    ep_rate: int = None
    location: str = None

class data_struct:
    def __init__(self):
        self.role_queue: dict[str, RoleRequest] = {}
        self.roles: dict[str, RoleClass] = {}
        self.reaction_map: dict[str, int] = {}

class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = sqlite3.connect("bot_state.db")
        self.data = data_struct()
        self.react_message_id: int = None
        self.data_loaded = False
        self._init_db()

    def _init_db(self):
        cursor = self.db.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.db.commit()

    def load_data(self):
        cursor = self.db.cursor()
        cursor.execute("SELECT key, value FROM state")
        
        saved_state = {
            row[0]: json.loads(row[1]) if row[1] is not None else None 
            for row in cursor.fetchall()
        }

        raw_roles = saved_state.get("roles", {})
        raw_role_queue = saved_state.get("role_queue", {})

        self.data.roles = {
            role_name: RoleClass(**data) 
            for role_name, data in raw_roles.items()
        }
        self.data.role_queue = {
            role: RoleRequest(**data) 
            for role, data in raw_role_queue.items()
        }
        self.data.reaction_map = saved_state.get("reaction_map", {})

        self.react_message_id = saved_state.get("react_message_id", None)
        if self.react_message_id is not None: 
            self.react_message_id = int(self.react_message_id)

        self.data_loaded = True

    def save_data(self):
        cursor = self.db.cursor()

        serializable_queue = {
            role: asdict(request_obj) 
            for role, request_obj in self.data.role_queue.items()
        }

        serializable_roles = {
            role_name: asdict(role_obj) 
            for role_name, role_obj in self.data.roles.items()
        }
        
        cursor.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("role_queue", json.dumps(serializable_queue, default=json_datetime_serializer)))
        cursor.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("roles", json.dumps(serializable_roles)))
        cursor.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("reaction_map", json.dumps(self.data.reaction_map)))
        cursor.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("react_message_id", json.dumps(self.react_message_id)))
        
        self.db.commit()

    async def setup_hook(self):
        self.load_data()
        await self.tree.sync()

    async def close(self):
        if self.data_loaded:
            self.save_data()
        self.db.close()
        await super().close()

intents = discord.Intents.default()
bot = MyBot(command_prefix="!", intents=intents)