import aiosqlite
import json
import discord
from discord.ext import commands
from dataclasses import asdict, dataclass
import datetime
from aiohttp import web
import aiohttp
import os

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
    ep_progress: int = 0
    total_eps: int = None
    ep_rate: int = 1
    emoji: str = None

@dataclass
class RoleClass:
    role_id: int
    day: int = None
    time: str = None
    ping_notice: int = None
    ep_progress: int = None
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
        self.db: aiosqlite.Connection = None # Start as None, connect later
        self.data = data_struct()
        self.react_message_id: int = None
        self.data_loaded = False

    async def _init_db(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS mal_tokens (
                user_id INTEGER PRIMARY KEY,
                access_token TEXT,
                refresh_token TEXT,
                expires_at INTEGER
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS mal_auth_cache (
                user_id INTEGER PRIMARY KEY,
                code_verifier TEXT
            )
        """)
        await self.db.commit()

    async def load_data(self):
        async with self.db.execute("SELECT key, value FROM state") as cursor:
            rows = await cursor.fetchall()
        
        saved_state = {
            row[0]: json.loads(row[1]) if row[1] is not None else None 
            for row in rows
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

    async def save_data(self):
        if not self.db:
            return

        serializable_queue = {
            role: asdict(request_obj) 
            for role, request_obj in self.data.role_queue.items()
        }
        serializable_roles = {
            role_name: asdict(role_obj) 
            for role_name, role_obj in self.data.roles.items()
        }
        
        await self.db.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("role_queue", json.dumps(serializable_queue, default=json_datetime_serializer)))
        await self.db.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("roles", json.dumps(serializable_roles)))
        await self.db.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("reaction_map", json.dumps(self.data.reaction_map)))
        await self.db.execute("REPLACE INTO state (key, value) VALUES (?, ?)", 
                       ("react_message_id", json.dumps(self.react_message_id)))
        
        await self.db.commit()

    async def setup_hook(self):
        # Connect to the DB asynchronously when the bot starts
        self.db = await aiosqlite.connect("bot_state.db")
        await self._init_db()
        await self.load_data()
        await self.tree.sync()
        await self.start_web_server()

    async def close(self):
        if self.data_loaded:
            await self.save_data()
        if self.db:
            await self.db.close()
        await super().close()

    async def start_web_server(self):
        app = web.Application()
        app.add_routes([web.get('/callback', self.mal_callback)])
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Listen on port 8000, (Tailscale Funnel)
        site = web.TCPSite(runner, '0.0.0.0', 8000)
        await site.start()
        print("Web server running on port 8000 for MAL Auth")

    async def mal_callback(self, request: web.Request):
        # Catch MAL's response
        code = request.query.get('code')
        state = request.query.get('state') # the Discord User ID

        if not code or not state:
            return web.Response(text="Missing code or state. Login failed.", status=400)

        user_id = int(state)

        # PKCE verifier
        async with self.db.execute("SELECT code_verifier FROM mal_auth_cache WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            return web.Response(text="Auth session expired or invalid. Try running /mal_login again.", status=400)
        
        code_verifier = row[0]

        # Ask MAL for the permanent tokens
        url = "https://myanimelist.net/v1/oauth2/token"
        data = {
            'client_id': os.getenv("MAL_CLIENT_ID"),
            'client_secret': os.getenv("MAL_CLIENT_SECRET"),
            'grant_type': 'authorization_code',
            'code': code,
            'code_verifier': code_verifier,
            'redirect_uri': os.getenv("REDIRECT_URI")
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                token_data = await resp.json()
                if resp.status != 200:
                    return web.Response(text=f"MAL Error: {token_data}", status=400)

        # Save tokens to database
        access_token = token_data['access_token']
        refresh_token = token_data['refresh_token']
        expires_in = token_data['expires_in']
        
        expires_at = int(datetime.datetime.now().timestamp()) + expires_in

        await self.db.execute(
            "REPLACE INTO mal_tokens (user_id, access_token, refresh_token, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, access_token, refresh_token, expires_at)
        )

        await self.db.execute("DELETE FROM mal_auth_cache WHERE user_id = ?", (user_id,))
        await self.db.commit()

        return web.Response(text="Login successful! You can close this window and return to Discord.")
    
    async def save_code_verifier(self, user_id, code_verifier):
        await self.db.execute(
            "REPLACE INTO mal_auth_cache (user_id, code_verifier) VALUES (?, ?)",
            (user_id, code_verifier)
        )
        await self.db.commit()

    async def get_valid_mal_token(self, user_id: int):
        async with self.db.execute("SELECT access_token, refresh_token, expires_at FROM mal_tokens WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            return None
            
        access_token, refresh_token, expires_at = row
        
        if int(datetime.datetime.now().timestamp()) + 300 >= expires_at:
            url = "https://myanimelist.net/v1/oauth2/token"
            data = {
                'client_id': os.getenv("MAL_CLIENT_ID"),
                'client_secret': os.getenv("MAL_CLIENT_SECRET"),
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data) as resp:
                    if resp.status == 200:
                        new_tokens = await resp.json()
                        access_token = new_tokens['access_token']
                        new_refresh = new_tokens['refresh_token']
                        expires_at = int(datetime.datetime.now().timestamp()) + new_tokens['expires_in']
                        
                        await self.db.execute(
                            "UPDATE mal_tokens SET access_token = ?, refresh_token = ?, expires_at = ? WHERE user_id = ?",
                            (access_token, new_refresh, expires_at, user_id)
                        )
                        await self.db.commit()
                    else:
                        return None
                        
        return access_token

    async def update_mal_episode(self, user_id: int, anime_name: str, current_ep: int):
        try:
            token = await self.get_valid_mal_token(user_id)
            if not token:
                return

            headers = {'Authorization': f'Bearer {token}'}
        
            async with aiohttp.ClientSession() as session:
                al_query = '''
                query ($search: String) {
                  Page (page: 1, perPage: 1) {
                    media (search: $search, type: ANIME, sort: SEARCH_MATCH) {
                      idMal
                      title { romaji }
                    }
                  }
                }
                '''
                async with session.post('https://graphql.anilist.co', json={'query': al_query, 'variables': {'search': anime_name}}) as al_resp:
                    if al_resp.status != 200:
                        print(f"[MAL Error] AniList API returned an error: {await al_resp.text()}")
                        return
                    
                    al_data = await al_resp.json()
                    
                    media_list = al_data.get('data', {}).get('Page', {}).get('media', [])
                    
                    if not media_list:
                        print(f"[MAL Error] Could not find '{anime_name}' on AniList to translate.")
                        return
                        
                    best_match = media_list[0]
                    anime_id = best_match.get('idMal')
                    mal_title = best_match.get('title', {}).get('romaji', 'Unknown')
                    
                    if not anime_id:
                        print(f"[MAL Error] AniList does not have a MAL ID linked for '{anime_name}'.")
                        return

                update_url = f"https://api.myanimelist.net/v2/anime/{anime_id}/my_list_status"
                update_data = {
                    'num_watched_episodes': current_ep,
                    'status': 'watching'
                }
                
                async with session.patch(update_url, data=update_data, headers=headers) as update_resp:
                    if update_resp.status not in [200, 201]:
                        print(f"[MAL Error] Update failed with status {update_resp.status}: {await update_resp.text()}")
        except Exception as e:
            import traceback
            print(f"[MAL Fatal Error] Exception in update_mal_episode for user {user_id}: {e}")
            traceback.print_exc()