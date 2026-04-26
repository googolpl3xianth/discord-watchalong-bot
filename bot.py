import discord
from discord import app_commands
from discord.ext import tasks, commands
import os
from dotenv import load_dotenv
from db import MyBot, RoleRequest, RoleClass
import emoji
from utils import parse_schedule, get_available_emoji, get_datetime, compare_weekday, check_ping_tracker
import zoneinfo
import datetime as dt
from datetime import timedelta
import asyncio
import aiohttp
import psutil
import secrets

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TICKET_CHANNEL_ID = int(os.getenv("TICKET_CHANNEL_ID"))
ROLE_CHANNEL_ID = int(os.getenv("ROLE_CHANNEL_ID"))
PING_CHANNEL_ID = int(os.getenv("PING_CHANNEL_ID"))
TIME_ZONE = os.getenv("TIME_ZONE")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = MyBot(command_prefix="$", intents=intents)

ping_tracker: dict[str, dt.datetime] = {}
day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@tasks.loop(hours=1)
async def print_memory():
    await bot.wait_until_ready()

    process = psutil.Process(os.getpid())
    mem_mb = process.memory_info().rss / (1024 ** 2)
    
    system_mem = psutil.virtual_memory().percent
    
    print(f"Bot Memory Usage: {mem_mb:.2f} MB")
    print(f"System Memory Usage: {system_mem}%")

@tasks.loop(seconds=60)
async def weekly_ping_task():
    await bot.wait_until_ready()
    
    now = dt.datetime.now(zoneinfo.ZoneInfo(TIME_ZONE))
    ping_channel = bot.get_channel(PING_CHANNEL_ID) 
    if not ping_channel:
        print(f"[ERROR] Ping channel not found at: {ping_channel}")
        return

    for role_name, role_data in list(bot.data.roles.items()):
        role = ping_channel.guild.get_role(role_data.role_id)
        if role is None or role_data.day is None or role_data.time is None or role_data.ep_progress is None or role_data.ep_rate is None or role_data.total_eps is None:
            continue

        target_dt_obj = get_datetime(role_data, now)
        if compare_weekday(target_dt_obj, now):
            last_ping = ping_tracker.get(role_name)
            if not check_ping_tracker(last_ping, target_dt_obj):
                if role_data.ep_progress >= role_data.total_eps:
                    await role.delete(reason="Anime Finished")
                    
                    key_to_del = next((k for k, v in bot.data.reaction_map.items() if v == role_data.role_id), None)
                    if key_to_del:
                        del bot.data.reaction_map[key_to_del]

                        role_channel = bot.get_channel(ROLE_CHANNEL_ID)
                        role_message = await role_channel.fetch_message(bot.react_message_id)
                        await role_message.clear_reaction(key_to_del)
                    
                    del bot.data.roles[role_name]
                    if role_name in ping_tracker:
                        del ping_tracker[role_name]
                    await update_role_message()
                    await bot.save_data()
                    continue
                ping_tracker[role_name] = target_dt_obj
                if role_data.ping_notice is not None:
                    message = (f"{role.mention} Reminder that we will be watching **{role_name}**")
                    if role_data.ep_rate > 1:
                        message += f" - Episodes {role_data.ep_progress+1}-{role_data.ep_progress+role_data.ep_rate}"
                    else:
                        message += f" - Episode {role_data.ep_progress+1}"
                    message += f" in {role_data.ping_notice} minutes"
                    if role_data.location is not None: message += f" at {role_data.location}!"
                    await ping_channel.send(message)
                role_data.ep_progress += role_data.ep_rate
                await bot.save_data()
                await update_role_message()
                for member in role.members:
                    asyncio.create_task(bot.update_mal_episode(member.id, role_name, role_data.ep_progress))
        else:
            print(f"{role_name}'s date {target_dt_obj} is not now {now}")

# Autocomplete 
async def queued_roles_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=role_name, value=role_name)
        for role_name in bot.data.role_queue.keys()
        if current.lower() in role_name.lower()
    ]
    return choices[:25]

async def watchalong_roles_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=role_name, value=role_name)
        for role_name in bot.data.roles.keys()
        if current.lower() in role_name.lower()
    ]
    return choices[:25]


anilist_cache = {}
async def anilist_search_autocomplete(
    interaction: discord.Interaction, 
    current: str
) -> list[app_commands.Choice[str]]:
    if len(current) < 3:
        return []
    
    if current in anilist_cache:
        return anilist_cache[current]

    url = 'https://graphql.anilist.co'
    query = '''
    query ($search: String) {
      Page (page: 1, perPage: 10) {
        media (search: $search, type: ANIME, sort: SEARCH_MATCH) {
          id
          title { romaji english }
          episodes
        }
      }
    }
    '''
    variables = {'search': current}

    timeout = aiohttp.ClientTimeout(total=1.2)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={'query': query, 'variables': variables}) as response:
                if response.status == 200:
                    data = await response.json()
                    anime_list = data['data']['Page']['media']
                    
                    choices = []
                    for anime in anime_list:
                        title = anime['title']['english'] or anime['title']['romaji']
                        episodes = anime['episodes'] or "Unknown"

                        display_name = f"{title[:80]} ({episodes} Eps)" 
                        hidden_value = f"{title[:80]}|eps:{episodes}"
                        
                        choices.append(app_commands.Choice(name=display_name, value=hidden_value))
                    anilist_cache[current] = choices
                    if len(anilist_cache) > 500:
                        anilist_cache.clear()

                    return choices
    except (asyncio.TimeoutError, aiohttp.ClientError):
        pass
    return []

@bot.event
async def on_ready():
    print("Bot online")
    
    for guild in bot.guilds:
        print(f"Loaded {len(guild.members)} members for {guild.name}")
        
    if bot.react_message_id is None:
        await init_react_message()
    await update_role_message()

    if not weekly_ping_task.is_running():
        weekly_ping_task.start()
        print("Weekly ping loop started!")

    if not print_memory.is_running():
        print_memory.start()

# user cmds
@bot.tree.command(name="rq", description="Request a new anime watchalong role")
@app_commands.describe(
    role_name="The name of the role (autofills from anilist database)",
    day=f"Day of week of meeting(e.g., mon, tue in {TIME_ZONE})",
    time=f"Time of day of meeting(e.g., 14:30, 2:30 PM in {TIME_ZONE})",
    ping_notice="Notice relative to time of meeting in minutes, defaults to no ping",
    location="Description for where meeting is",
    react_emoji="Emoji for the reaction",
    ep_progress="Starting amount of episodes watched, defaults to 0, for edge cases (ie ep 0/prologue), just leave as 0",
    total_eps="Overrides total episode of anime, defaults to what anilist finds or 1 if unable to find",
    ep_rate="Number of episodes watching per meeting, defaults to 1"
)
@app_commands.autocomplete(role_name=anilist_search_autocomplete)
async def request_role(interaction: discord.Interaction,
    role_name: str, 
    day: str = None,
    time: str = None, 
    ping_notice: int = None,
    location: str = None,
    react_emoji: str = None,
    ep_progress: int = 0,
    total_eps: int = 1,
    ep_rate: int = 1,
):
    await interaction.response.defer()

    user = interaction.user
    channel = bot.get_channel(TICKET_CHANNEL_ID)

    if not channel:
        await interaction.followup.send("Failure to find channel", ephemeral=True)
        return
    if(role_name in bot.data.roles):
        await interaction.followup.send(f"Role {role_name} already exists: {bot.data.roles[role_name]}", ephemeral=True)
        return
    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
    if existing_role:
        await interaction.followup.send(f"❌ A role named `{role_name}` already exists in this server", ephemeral=True)
        return
    if len(bot.data.roles) >= 20:
        await interaction.followup.send("❌ The role menu is full! (Discord limits messages to 20 reactions). Please request an admin to remove old roles first.", ephemeral=True)
        return
    
    if "|eps:" in role_name:
        actual_role_name, eps_string = role_name.rsplit("|eps:", 1)
        role_name = actual_role_name.strip()
        
        if eps_string != "Unknown" and eps_string.isdigit():
            total_eps = int(eps_string)

    if len(role_name) > 100:
        await interaction.followup.send("❌ Role names cannot be longer than 100 characters.", ephemeral=True)
        return
    
    day_int, parsed_time = parse_schedule(day, time)
    if(not react_emoji):
        react_emoji = get_available_emoji(bot)
    elif(not emoji.is_emoji(react_emoji)):
        await interaction.followup.send(f"Is not valid emoji", ephemeral=True)

    if day_int == -1:
        await interaction.followup.send(f"❌ I didn't understand the day `{day}`. Please use abbreviations like 'Mon', 'Tue', etc.", ephemeral=True)
        return
    if parsed_time == "err":
        await interaction.followup.send(f"❌ I didn't understand the time `{time}`. Try formats like `14:30` or `2:30 PM`.", ephemeral=True)
        return

    bot.data.role_queue[role_name] = RoleRequest(
        requester_id=user.id,
        day=day_int,
        time=parsed_time.isoformat() if parsed_time else None,
        ping_notice=ping_notice,
        location=location,
        ep_progress=ep_progress,
        total_eps=total_eps,
        ep_rate=ep_rate,
        emoji=react_emoji
    )
    await bot.save_data()

    day_str = "n/a"
    time_str = "n/a"
    if day_int is not None:
        global day_names
        day_str = day_names[bot.data.role_queue[role_name].day]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")

    await interaction.followup.send(f"Successfully requested the role **{role_name}**!", ephemeral=True)

    message = (
        f"**New Role Request from <@{user.id}>**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ep_progress is not None:
        message += f"Starting at episode progress `{bot.data.role_queue[role_name].ep_progress}`"
        if total_eps is not None:
            message += f" out of `{bot.data.role_queue[role_name].total_eps}` episodes"
        if ep_rate is not None:
            message += f" watching `{bot.data.role_queue[role_name].ep_rate}` per meeting"
        message += "\n"
    if day_str or time_str:
        message += f"**Time:** Every `{day_str}` at `{time_str}` in `{TIME_ZONE}`\n"
        if ping_notice is not None:
            message += f"**Ping:** `{bot.data.role_queue[role_name].ping_notice}` minutes before meeting\n"
    if location:
        message += f"**Location:** {bot.data.role_queue[role_name].location}\n"

    message += f"\nAdmins: Use `/addq {role_name}` or `/rmq {role_name}` to accept or deny."
    await channel.send(message, allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.command(name="mal_login", description="Link your MyAnimeList account to the bot")
async def mal_login(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if await bot.get_valid_mal_token(interaction.user.id) is not None:
        await interaction.followup.send("MyAnimeList account already linked", ephemeral=True)
        return
    client_id = os.getenv("MAL_CLIENT_ID")
    redirect_uri = os.getenv("REDIRECT_URI")

    if not client_id:
        await interaction.followup.send("The bot owner has not set up MAL API keys yet.", ephemeral=True)
        return

    # MAL requires a secure, random string 43 to 128 characters long
    code_verifier = secrets.token_urlsafe(100)[:128]
    
    await bot.save_code_verifier(interaction.user.id, code_verifier)
    
    auth_url = (
        f"https://myanimelist.net/v1/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&code_challenge={code_verifier}"
        f"&code_challenge_method=plain"
        f"&state={interaction.user.id}" 
        f"&redirect_uri={redirect_uri}"
    )

    await interaction.followup.send(
        f"Click [here]({auth_url}) to authorize the bot with your MyAnimeList account if you want automatic list updates", 
        ephemeral=True
    )

# admin cmds
@bot.tree.command(name="addq", description="Approve a role from the queue")
@app_commands.describe(
    role_name="The name of the role, Leave blank to approve the most recent request",
    day=f"Overwrites requet's day of week for meeting (e.g., mon, tue in `{TIME_ZONE}`)",
    time=f"Overwrites requet's time for meeting (e.g., 14:30, 2:30 PM in `{TIME_ZONE}`)",
    ping_notice="Overwrites notice relative to time of meeting in minutes, defaults to no ping",
    location="Overwrites description for where meeting is",
    react_emoji="Overwrites requet's emoji for the reaction",
    ep_progress="Overwrites starting episode progress, for edge cases (ie episode 0/prologue), just set to 0",
    total_eps="Overrides total episode of anime",
    ep_rate="Overrides number of episodes watching per meeting"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=queued_roles_autocomplete)
async def addq(
    interaction: discord.Interaction, 
    role_name: str = None, 
    day: str = None, 
    time: str = None, 
    ping_notice: int = None,
    location: str = None,
    react_emoji: str = None,
    ep_progress: int = None,
    total_eps: int = None,
    ep_rate: int = None,
):
    await interaction.response.defer()
    if(not role_name):
        if(bot.data.role_queue):
            role_name = list(bot.data.role_queue.keys())[-1]
        else:
            await interaction.followup.send("Queue is empty", ephemeral=True)
            return
    
    if role_name not in bot.data.role_queue:
        await interaction.followup.send(f"No request by that name. Queue:\n{list(bot.data.role_queue.keys())}", ephemeral=True)
        return

    if role_name in bot.data.roles:
        await interaction.followup.send(f"Role `{role_name}` already exists!", ephemeral=True)
        return
    
    global day_names

    request_data = bot.data.role_queue[role_name]
    if day is not None or time is not None:
        day_to_parse = day if day is not None else day_names[request_data.day]
        time_to_parse = time if time is not None else dt.time.fromisoformat(request_data.time).strftime("%H:%M")
        
        day_int, parsed_time = parse_schedule(day_to_parse, time_to_parse)
        
        if day_int is not None:
            request_data.day = day_int
        if parsed_time is not None:
            request_data.time = parsed_time.isoformat()
    
    if ping_notice is None: ping_notice = request_data.ping_notice
    if location is None: location = request_data.location
    if react_emoji is None: react_emoji = request_data.emoji
    if ep_progress is None: ep_progress = request_data.ep_progress
    if total_eps is None: total_eps = request_data.total_eps
    if ep_rate is None: ep_rate = request_data.ep_rate
    
    perms = discord.Permissions(send_messages=True, read_messages=True)
    role = await interaction.guild.create_role(
        name=role_name, 
        colour=discord.Colour.blue(), 
        permissions=perms,
        mentionable=True,
        hoist=False
    )
    bot.data.roles[role_name] = RoleClass(
        role_id=role.id,
        day=request_data.day,
        time=request_data.time,
        ping_notice=ping_notice,
        location=location,
        ep_progress=ep_progress,
        total_eps=total_eps,
        ep_rate=ep_rate,
    )

    bot.data.reaction_map[react_emoji] = role.id

    day_str = None
    time_str = None
    if(request_data.day is not None and request_data.time is not None and ping_notice is not None):
        day_str = day_names[bot.data.roles[role_name].day]
        dt_obj = dt.time.fromisoformat(bot.data.roles[role_name].time)
        time_str = dt_obj.strftime("%I:%M %p")
    del bot.data.role_queue[role_name]
    await update_role_message()
    await bot.save_data()

    message = (
        f"**New Role Added, requested by <@{request_data.requester_id}>**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ep_progress is not None:
        message += f"Episode progress `{bot.data.roles[role_name].ep_progress}`"
        if total_eps is not None:
            message += f" out of `{bot.data.roles[role_name].total_eps}` episodes"
        if ep_rate is not None:
            message += f" watching `{bot.data.roles[role_name].ep_rate}` per meeting"
        message += "\n"
    if day_str or time_str:
        message += f"**Time:** Every `{day_str}` at `{time_str}` in `{TIME_ZONE}`\n"
        if ping_notice is not None:
            message += f"**Ping:** `{bot.data.roles[role_name].ping_notice}` minutes before meeting\n"
    if location:
        message += f"**Location:** {bot.data.roles[role_name].location}\n"

    await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))


@bot.tree.command(name="rmq", description="Remove request from queue")
@app_commands.describe(role_name="Leave blank to deny the most recent request")
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=queued_roles_autocomplete)
async def rmq(interaction: discord.Interaction, role_name: str = None, ):
    await interaction.response.defer()
    if(not role_name and bot.data.role_queue):
        role_name = list(bot.data.role_queue.keys())[-1]
    if(role_name in bot.data.role_queue):
        del bot.data.role_queue[role_name]
        await bot.save_data()
        await interaction.followup.send(f"Successfully removed {role_name}", ephemeral=True)
    else:
        await interaction.followup.send(f"No request by that name, list:\n{list(bot.data.role_queue.keys())}", ephemeral=True)

@bot.tree.command(name="listq", description="Displays request queue")
@app_commands.default_permissions(manage_roles=True)
async def listq(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(f"list queue:\n{bot.data.role_queue}", ephemeral=True)

@bot.tree.command(name="add", description="Adds a role, bypassing the queue")
@app_commands.describe(
    role_name="The name of the role",
    day=f"Day of week of meeting(e.g., mon, tue in `{TIME_ZONE}`)",
    time=f"Time of day of meeting(e.g., 14:30, 2:30 PM in `{TIME_ZONE}`)",
    ping_notice="Notice relative to time of meeting in minutes, defaults to no ping",
    location="Description for where meeting is",
    react_emoji="Emoji for the reaction",
    ep_progress="Starting episode progress, defaults to 0, for edge cases (ie episode 0/prologue), just leave as 0",
    total_eps="Overrides total episode of anime, defaults to what anilist finds",
    ep_rate="Number of episodes watching per meeting, defaults to 1"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=anilist_search_autocomplete)
async def add(
    interaction: discord.Interaction, 
    role_name: str, 
    day: str = None, 
    time: str = None, 
    ping_notice: int = None,
    location: str = None,
    react_emoji: str = None,
    ep_progress: int = 0,
    total_eps: int = 1,
    ep_rate: int = 1,
):
    await interaction.response.defer()
    if not role_name:
        await interaction.followup.send("No role name detected", ephemeral=True)
        return
    if(role_name in bot.data.roles):
        await interaction.followup.send(f"Role {role_name} already exists: {bot.data.roles[role_name]}", ephemeral=True)
        return
    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
    if existing_role:
        await interaction.followup.send(f"❌ A role named `{role_name}` already exists in this server", ephemeral=True)
        return
    if len(bot.data.roles) >= 20:
        await interaction.followup.send("❌ The role menu is full! (Discord limits messages to 20 reactions). Please remove old roles first.", ephemeral=True)
        return
    
    if "|eps:" in role_name:
        actual_role_name, eps_string = role_name.rsplit("|eps:", 1)
        role_name = actual_role_name.strip()
        
        if eps_string != "Unknown" and eps_string.isdigit():
            total_eps = int(eps_string)

    if len(role_name) > 100:
        await interaction.followup.send("❌ Role names cannot be longer than 100 characters.", ephemeral=True)
        return
    
    day_int, parsed_time = parse_schedule(day, time)

    if day_int == -1:
        await interaction.followup.send(f"❌ I didn't understand the day `{day}`. Please use abbreviations like 'Mon', 'Tue', etc.")
        return
    if parsed_time == "err":
        await interaction.followup.send(f"❌ I didn't understand the time `{time}`. Try formats like `14:30` or `2:30 PM`.")
        return

    if(not react_emoji):
        react_emoji = get_available_emoji(bot)
    elif(not emoji.is_emoji(react_emoji)):
        await interaction.followup.send(f"Is not valid emoji", ephemeral=True)

    perms = discord.Permissions(send_messages=True, read_messages=True)
    role = await interaction.guild.create_role(
        name=role_name, 
        colour=discord.Colour.blue(), 
        permissions=perms,
        mentionable=True,
        hoist=False
    )
    bot.data.roles[role_name] = RoleClass(
        role_id=role.id,
        day=day_int,
        time=parsed_time.isoformat() if parsed_time else None,
        ping_notice=ping_notice,
        location=location,
        ep_progress=ep_progress,
        total_eps=total_eps,
        ep_rate=ep_rate,
    )

    bot.data.reaction_map[react_emoji] = role.id
    await update_role_message()
    await bot.save_data()

    day_str = None
    time_str = None
    if day_int is not None:
        global day_names
        day_str = day_names[bot.data.roles[role_name].day]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")

    message = (
        f"**<@{interaction.user.id}> created New Role**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ep_progress is not None:
        message += f"Starting episode progress `{bot.data.roles[role_name].ep_progress}`"
        if total_eps is not None:
            message += f" out of `{bot.data.roles[role_name].total_eps}` episodes"
        if ep_rate is not None:
            message += f" watching `{bot.data.roles[role_name].ep_rate}` per meeting"
        message += "\n"
    if day_str and time_str:
        message += f"**Time:** Every `{day_str}` at `{time_str}` in `{TIME_ZONE}`\n"
        if ping_notice is not None:
            message += f"**Ping:** `{bot.data.roles[role_name].ping_notice}` minutes before meeting\n"
    if location:
        message += f"**Location:** {bot.data.roles[role_name].location}\n"
    await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.command(name="rm", description="Removes a watchalong role")
@app_commands.describe(
    role_name="Name of target role"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=watchalong_roles_autocomplete)
async def rm(interaction: discord.Interaction, role_name: str):
    await interaction.response.defer()
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    if role_name not in bot.data.roles:
        await interaction.followup.send(f"Role must be a watchalong role, list: {list(bot.data.roles.keys())}", ephemeral=True)
        return
    del bot.data.roles[role_name]
    if role_name in ping_tracker:
        del ping_tracker[role_name]
    if role is None:
        await interaction.followup.send(f"[Warning] Role is not in server, but {role_name} was deleted", ephemeral=True)
        return
    key_to_del = next((k for k, v in bot.data.reaction_map.items() if v == role.id), None)
    await role.delete(reason=f"Deleted by {interaction.user.name}")
    if key_to_del:
        del bot.data.reaction_map[key_to_del]
        role_channel = bot.get_channel(int(ROLE_CHANNEL_ID))
        if role_channel:
            try:
                try:
                    role_message = await role_channel.fetch_message(bot.react_message_id)
                except Exception as e:
                    await init_react_message()
                    role_message = await role_channel.fetch_message(bot.react_message_id)
                await role_message.clear_reaction(key_to_del)
            except Exception as e:
                print(f"[WARNING] Could not clear reactions for {key_to_del}: {e}")

        await update_role_message()
        await bot.save_data()
    await interaction.followup.send(f"The role {role.name} has been deleted by <@{interaction.user.id}>.", allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.command(name="list", description="Displays All Watchalong Roles")
@app_commands.default_permissions(manage_roles=True)
async def listroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(f"role list:\n{bot.data.roles}", ephemeral=True)

@bot.tree.command(name="edit", description="Edits the data of an existing role")
@app_commands.describe(
    role_name="The name of the role",
    day=f"Day of week of meeting(e.g., mon, tue in `{TIME_ZONE}`)",
    time=f"Time of day of meeting(e.g., 14:30, 2:30 PM in `{TIME_ZONE}`)",
    ping_notice="Notice relative to time of meeting in minutes, and negative ping_notice means no ping",
    location="Description for where meeting is",
    react_emoji="Emoji for the reaction, (note: changing the emoji removes the old_emoji, users will retain their role but the new_emoji will not accurately reflect their role)",
    ep_progress="Current episode progress (how many episode we have completed)",
    total_eps="Total episode of anime",
    ep_rate="Number of episodes watching per meeting"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=watchalong_roles_autocomplete)
async def edit_role(
    interaction: discord.Interaction, 
    role_name: str, 
    day: str = None, 
    time: str = None, 
    ping_notice: int = None,
    location: str = None,
    react_emoji: str = None,
    ep_progress: int = None,
    total_eps: int = None,
    ep_rate: int = None,
):
    await interaction.response.defer()
    if not role_name:
        await interaction.followup.send("No role name detected", ephemeral=True)
        return
    if role_name not in bot.data.roles:
        available_roles = list(bot.data.roles.keys())
        await interaction.followup.send(f"Role {role_name} not found, existing list: {available_roles}", ephemeral=True)
        return
    role_name = role_name.strip()
    old_ep_progress = bot.data.roles[role_name].ep_progress
    old_total_eps = bot.data.roles[role_name].total_eps
    old_ep_rate = bot.data.roles[role_name].ep_rate
    old_day = bot.data.roles[role_name].day
    old_time = bot.data.roles[role_name].time
    old_ping_notice = bot.data.roles[role_name].ping_notice
    old_location = bot.data.roles[role_name].location
    old_day_str = "n/a"
    old_time_str = "n/a"
    global day_names
    if old_day is not None:
        old_day_str = day_names[old_day]
    if old_time is not None:
        time_obj = dt.time.fromisoformat(old_time)
        old_time_str = time_obj.strftime("%I:%M %p")
    
    day_int, parsed_time = parse_schedule(day, time)

    if day_int == -1:
        await interaction.followup.send(f"❌ I didn't understand the day `{day}`. Please use abbreviations like 'Mon', 'Tue', etc.", ephemeral=True)
        return
    if parsed_time == "err":
        await interaction.followup.send(f"❌ I didn't understand the time `{time}`. Try formats like `14:30` or `2:30 PM`.", ephemeral=True)
        return

    if day_int is not None: bot.data.roles[role_name].day = day_int
    if time is not None: bot.data.roles[role_name].time = parsed_time.isoformat()
    if ping_notice is not None: 
        if ping_notice < 0:
            bot.data.roles[role_name].ping_notice = None
        else:
            bot.data.roles[role_name].ping_notice = ping_notice
    if location is not None: bot.data.roles[role_name].location = location
    if ep_progress is not None: bot.data.roles[role_name].ep_progress = ep_progress
    if total_eps is not None: bot.data.roles[role_name].total_eps = total_eps
    if ep_rate is not None: bot.data.roles[role_name].ep_rate = ep_rate

    day_str = None
    time_str = None
    if bot.data.roles[role_name].day:
        day_str = day_names[bot.data.roles[role_name].day]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")
    elif bot.data.roles[role_name].time:
        time_obj = dt.time.fromisoformat(bot.data.roles[role_name].time)
        time_str = time_obj.strftime("%I:%M %p")

    message = (
        f"**<@{interaction.user.id}> updated role: `{role_name}`**\n"
    )

    if react_emoji is not None:
        if(not emoji.is_emoji(react_emoji)):
            await interaction.followup.send(f"Is not valid emoji", ephemeral=True)
        else:
            old_emoji = next((k for k, v in bot.data.reaction_map.items() if v == bot.data.roles[role_name].role_id), None)
            if old_emoji is None:
                print(f"[ERROR] No pair in reaction_map with role {role_name}, {bot.data.reaction_map}")
                return
            await move_reacts(old_emoji, react_emoji)
            bot.data.reaction_map[react_emoji] = bot.data.roles[role_name].role_id
            del bot.data.reaction_map[old_emoji]
            message += f"Changed Reaction Emoji from {old_emoji}->{react_emoji}"

    await update_role_message()
    await bot.save_data()

    if ep_progress is not None and (ep_progress != old_ep_progress):
        message += f"**Current episode progress** `{old_ep_progress}`->`{bot.data.roles[role_name].ep_progress}`\n"
    else:
        message += f"**Current episode progress** `{bot.data.roles[role_name].ep_progress}`\n"
    if total_eps is not None and (total_eps != old_total_eps):
        message += f"**Total episodes** `{old_total_eps}`->`{bot.data.roles[role_name].total_eps}`\n"
    else:
        message += f"**Total episodes** `{bot.data.roles[role_name].total_eps}`\n"
    if ep_rate is not None and (ep_rate != old_ep_rate):
        message += f"**Episode rate** `{old_ep_rate}`->`{bot.data.roles[role_name].ep_rate}` per meeting\n"
    else:
        message += f"**Episode rate** `{bot.data.roles[role_name].ep_rate} per meeting`\n"
    message += f"**Time:** Every "
    if(day is not None): message += f"`{old_day_str}` -> "
    message += f"`{day_str}` at "
    if(time is not None): message += f"`{old_time_str}` -> "
    message += f"`{time_str}` in `{TIME_ZONE}`\n"
    if ping_notice is not None and (old_ping_notice != ping_notice):
        message += f"**Ping:** `{old_ping_notice}` -> `{bot.data.roles[role_name].ping_notice}` minutes before meeting\n"
    else:
        message += f"**Ping:** `{bot.data.roles[role_name].ping_notice}` minutes before meeting\n"
    if location is not None and (location != old_location):
        message += f"**Location:** {old_location} -> {bot.data.roles[role_name].location}\n"
    else:
        message += f"**Location:** {bot.data.roles[role_name].location}\n"
    await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.command(name="pings", description="Lists ping history (Only tracks latest ping, a future ping indicates that ping will be skipped)")
@app_commands.default_permissions(manage_roles=True)
async def pings(interaction: discord.Interaction):
    message = "ping history:\n"
    for role_name, datetime_str in ping_tracker.items():
        message += f"`{role_name}`: `{datetime_str}`\n"
    await interaction.response.send_message(message, ephemeral=True)

@bot.tree.command(name="skip", description="Skips the next ping for the specified role (iff the ping time is the same as when this is called)")
@app_commands.describe(
    role_name="name of target role"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=watchalong_roles_autocomplete)
async def skip(interaction: discord.Interaction, role_name: str):
    await interaction.response.defer()
    if role_name not in bot.data.roles:
        available_roles = list(bot.data.roles.keys())
        await interaction.followup.send(f"Warning, no role {role_name} in list: {available_roles}", ephemeral=True)
    now = dt.datetime.now(zoneinfo.ZoneInfo(TIME_ZONE))

    role_data = bot.data.roles[role_name]
    if role_data.ping_notice is None or role_data.day is None or role_data.time is None:
        await interaction.followup.send(f"Warning, no ping notice/day/time set for {role_name}: {role_data}", ephemeral=True)
    time_obj = dt.time.fromisoformat(role_data.time)
    temp_days = role_data.day-now.weekday()
    if temp_days < 0 or (temp_days == 0 and now.time() > time_obj): temp_days+=7
    target_date = now + timedelta(days=temp_days)
    dt_obj = dt.datetime.combine(target_date, time_obj)
    target_dt_obj = dt_obj - timedelta(minutes=role_data.ping_notice)
    ping_tracker[role_name] = target_dt_obj

    formatted = target_dt_obj.strftime("%A %I:%M %p")
    await interaction.followup.send(f"<@{interaction.user.id}> skiping planned ping `{formatted}`", allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingAnyRole):
        await interaction.response.send_message("❌ You do not have permission to use this command. Avaliable cmds include /rq /listq /list or ask an admin for approval", ephemeral=True)
    else:
        print(error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred while processing the command.", ephemeral=True)
            else:
                await interaction.followup.send("An error occurred while processing the command.", ephemeral=True)
        except discord.HTTPException:
            pass

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Ignoring traditional command error: {error}")

@weekly_ping_task.before_loop
async def before_minute_task():
    # Wait until the start of the next minute
    now = dt.datetime.now()
    wait_time = 60 - now.second
    await asyncio.sleep(wait_time)

@bot.event
async def on_raw_reaction_add(payload):
    if payload.member.bot: return
    if payload.message_id != bot.react_message_id:
        return
    if str(payload.emoji) in bot.data.reaction_map:
        guild = bot.get_guild(payload.guild_id)
        role_id = bot.data.reaction_map[str(payload.emoji)]
        role = guild.get_role(role_id)

        if role:
            await payload.member.add_roles(role)
        else:
            print(f"[ERROR] Could not find role from role id: {role_id} for reaction: {payload.emoji}")

@bot.event
async def on_raw_reaction_remove(payload):
    user = bot.get_user(payload.user_id) 

    if user and user.bot:
        return
    if payload.message_id != bot.react_message_id:
        return

    if str(payload.emoji) in bot.data.reaction_map:
        guild = bot.get_guild(payload.guild_id)
        role_id = bot.data.reaction_map[str(payload.emoji)]
        role = guild.get_role(role_id)

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            member = None
        if role and member:
            await member.remove_roles(role)
        else:
            print(f"[ERROR] Could not find role and/or member from role id: {role_id} member_id {payload.user_id} for reaction: {payload.emoji}")

async def update_role_message():
    channel = bot.get_channel(ROLE_CHANNEL_ID)
    try:
        msg = await channel.fetch_message(bot.react_message_id)
    except Exception as e:
        await init_react_message()
        msg = await channel.fetch_message(bot.react_message_id)

    message = (
        f"**Role Menu: Anime Watchalongs**\n"
        f"React to give yourself a role.\n"
    )
    global day_names
    for emoji, role_id in bot.data.reaction_map.items():
        role = channel.guild.get_role(role_id)
        if not role:
            continue
        role_info = bot.data.roles.get(role.name)
        message += (f"\n{emoji} : `{role.name}`")
        if role_info and role_info.day is not None and role_info.time:
            time_obj = dt.time.fromisoformat(role_info.time)
            formatted_time = time_obj.strftime("%I:%M %p")
            temp = f" {role_info.location}" if role_info.location else ""
            message += f" on `{day_names[role_info.day]}` at `{formatted_time}`{temp} with `{role_info.ping_notice}` minute notice"
        if role_info.ep_progress is not None and role_info.total_eps is not None and role_info.ep_rate:
            message += f" current episode progress `{role_info.ep_progress}/{role_info.total_eps}`"
        message += "\n"

    await msg.edit(content=message)

    bot_reactions = [str(r.emoji) for r in msg.reactions if r.me]

    for emoji_str in bot.data.reaction_map.keys():
        if emoji_str not in bot_reactions:
            try:
                await msg.add_reaction(emoji_str)
            except Exception as e:
                print(f"[WARNING] Could not add reaction {emoji_str}: {e}")

async def init_react_message():
    channel = bot.get_channel(ROLE_CHANNEL_ID)
    message = await channel.send(f"**Role Menu: Anime Watchalongs**\n"
                        f"React to give yourself a role.\n")
    bot.react_message_id = message.id
    await bot.save_data()

async def move_reacts(old_emoji, new_emoji):
    channel = bot.get_channel(ROLE_CHANNEL_ID)
    try:
        message = await channel.fetch_message(bot.react_message_id)
    except discord.NotFound:
        await init_react_message()
        message = await channel.fetch_message(bot.react_message_id)

    old_reaction = discord.utils.get(message.reactions, emoji=old_emoji)
    if not old_reaction:
        print("[ERROR] Old emoji reaction not found on this message.")
        return

    await message.add_reaction(new_emoji)

    await message.clear_reaction(old_emoji)

bot.run(BOT_TOKEN)