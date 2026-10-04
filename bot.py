import discord
from discord import app_commands
from discord.ext import tasks, commands
import os
from dotenv import load_dotenv
from db import MyBot, RoleRequest, RoleClass, GuildData
import emoji
from utils import parse_schedule, get_available_emoji, get_datetime, compare_weekday, check_ping_tracker
import zoneinfo
import datetime as dt
from datetime import timedelta
import asyncio
import aiohttp
import psutil
import secrets
import re

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
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

    for guild_id, guildData in bot.data.guilds.items():
        ping_channel = bot.get_channel(guildData.ping_channel_id) 
        if not ping_channel:
            try:
                ping_channel = await bot.fetch_channel(guildData.ping_channel_id)
            except Exception:
                ping_channel = None
        if ping_channel is None:
            print(f"[Warning] Ping channel {guildData.ping_channel_id} unreachable for guild {guild_id}")
            continue
        for role_name, role_data in list(guildData.roles.items()):
            role = ping_channel.guild.get_role(role_data.role_id)
            if role is None or role_data.day is None or role_data.time is None or role_data.ep_progress is None or role_data.ep_rate is None or role_data.total_eps is None:
                continue

            target_dt_obj = get_datetime(role_data, now)

            if compare_weekday(target_dt_obj, now):
                last_ping = ping_tracker.get(role_name)
                if not check_ping_tracker(last_ping, target_dt_obj):
                    if role_data.ep_progress >= role_data.total_eps:
                        await role.delete(reason="Anime Finished")
                        
                        key_to_del = next((k for k, v in guildData.reaction_map.items() if v == role_data.role_id), None)
                        if key_to_del:
                            del guildData.reaction_map[key_to_del]

                            role_channel = bot.get_channel(guildData.role_channel_id)
                            role_message = await role_channel.fetch_message(guildData.react_message_id)
                            await role_message.clear_reaction(key_to_del)
                        
                        del guildData.roles[role_name]
                        if role_name in ping_tracker:
                            del ping_tracker[role_name]
                        await update_role_message(guild_id)
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
                    await update_role_message(guild_id)
                    if role_data.update_mal:
                        for member in role.members:
                            asyncio.create_task(bot.update_mal_episode(member.id, role_name, role_data.ep_progress))
            else:
                #print(f"{role_name}'s date {target_dt_obj} is not now {now}")
                pass

# Autocomplete 
async def queued_roles_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=role_name, value=role_name)
        for role_name in bot.data.guilds[interaction.guild.id].role_queue.keys()
        if current.lower() in role_name.lower()
    ]
    return choices[:25]

async def watchalong_roles_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name=role_name, value=role_name)
        for role_name in bot.data.guilds[interaction.guild.id].roles.keys()
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
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        print(f"An error occurred: {e}")
    return []

@bot.event
async def on_ready():
    print("Bot online")
    
    for guild in bot.guilds:
        print(f"Loaded {len(guild.members)} members for {guild.name}")

    for guild_id, guildData in bot.data.guilds.items():
        if guildData.react_message_id is None:
            await init_react_message(guild_id)
        await update_role_message(guild_id)

    if not weekly_ping_task.is_running():
        weekly_ping_task.start()
        print("Weekly ping loop started!")

    #if not print_memory.is_running():
    #    print_memory.start()

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
    ep_rate="Number of episodes watching per meeting, defaults to 1",
    continuation="Role name if it is a continuation of an existing role, defaults to none",
    update_mal="Set True if updates should be reflected in mal"
)
@app_commands.autocomplete(role_name=anilist_search_autocomplete)
@app_commands.autocomplete(continuation=watchalong_roles_autocomplete)
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
    continuation: str = None,
    update_mal: bool = True,
):
    await interaction.response.defer()
    guildData = bot.data.guilds[interaction.guild.id]

    user = interaction.user
    channel = bot.get_channel(guildData.ticket_channel_id)

    if not channel:
        await interaction.followup.send("Failure to find channel", ephemeral=True)
        return
    if(role_name in guildData.roles):
        await interaction.followup.send(f"Role {role_name} already exists: {guildData.roles[role_name]}", ephemeral=True)
        return
    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
    if existing_role:
        await interaction.followup.send(f"❌ A role named `{role_name}` already exists in this server", ephemeral=True)
        return
    if len(guildData.roles) >= 20:
        await interaction.followup.send("❌ The role menu is full! (Discord limits messages to 20 reactions). Please request an admin to remove old roles first.", ephemeral=True)
        return

    if continuation is not None:
        cont_role = discord.utils.get(interaction.guild.roles, name=continuation)
        if continuation not in guildData.roles:
            await interaction.followup.send(f"Role must be a watchalong role, list: {list(guildData.roles.keys())}", ephemeral=True)
            return
    
    if "|eps:" in role_name:
        actual_role_name, eps_string = role_name.rsplit("|eps:", 1)
        role_name = actual_role_name.strip()
        
        if eps_string != "Unknown" and eps_string.isdigit():
            total_eps = int(eps_string)
    else:
        match = re.search(r'\s*\(([0-9]+|Unknown)\s*Eps\)$', role_name, flags=re.IGNORECASE)
        if match:
            eps_string = match.group(1)
            role_name = re.sub(r'\s*\(([0-9]+|Unknown)\s*Eps\)$', '', role_name, flags=re.IGNORECASE).strip()
            
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

    guildData.role_queue[role_name] = RoleRequest(
        requester_id=user.id,
        day=day_int,
        time=parsed_time.isoformat() if parsed_time else None,
        ping_notice=ping_notice,
        location=location,
        ep_progress=ep_progress,
        total_eps=total_eps,
        ep_rate=ep_rate,
        emoji=react_emoji,
        update_mal=update_mal,
    )
    await bot.save_data()

    day_str = "n/a"
    time_str = "n/a"
    if day_int is not None:
        global day_names
        day_str = day_names[guildData.role_queue[role_name].day]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")

    await interaction.followup.send(f"Successfully requested the role **{role_name}**!", ephemeral=True)

    message = (
        f"**New Role Request from <@{user.id}>**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ep_progress is not None:
        message += f"Starting at episode progress `{guildData.role_queue[role_name].ep_progress}`"
        if total_eps is not None:
            message += f" out of `{guildData.role_queue[role_name].total_eps}` episodes"
        if ep_rate is not None:
            message += f" watching `{guildData.role_queue[role_name].ep_rate}` per meeting"
        message += "\n"
    if day_str or time_str:
        message += f"**Time:** Every `{day_str}` at `{time_str}` in `{TIME_ZONE}`\n"
        if ping_notice is not None:
            message += f"**Ping:** `{guildData.role_queue[role_name].ping_notice}` minutes before meeting\n"
    if location:
        message += f"**Location:** {guildData.role_queue[role_name].location}\n"

    if continuation is not None:
        message +=f"\n*This is a continuation of {cont_role}"

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
    ep_rate="Overrides number of episodes watching per meeting",
    continuation="Role name if it is a continuation of an existing role, defaults to none",
    update_mal="Set True if updates should be reflected in mal",
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=queued_roles_autocomplete)
@app_commands.autocomplete(continuation=watchalong_roles_autocomplete)
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
    continuation: str = None,
    update_mal: bool = True,
):
    await interaction.response.defer()

    guildData = bot.data.guilds[interaction.guild.id]
    if(not role_name):
        if(guildData.role_queue):
            role_name = list(guildData.role_queue.keys())[-1]
        else:
            await interaction.followup.send("Queue is empty", ephemeral=True)
            return
    
    if role_name not in guildData.role_queue:
        await interaction.followup.send(f"No request by that name. Queue:\n{list(guildData.role_queue.keys())}", ephemeral=True)
        return

    if role_name in guildData.roles:
        await interaction.followup.send(f"Role `{role_name}` already exists!", ephemeral=True)
        return
    
    global day_names

    request_data = guildData.role_queue[role_name]
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
    if continuation is None: continuation = request_data.contiuation

    cont_role = None
    if continuation is not None:
        cont_role = discord.utils.get(interaction.guild.roles, name=continuation)
        if continuation not in guildData.roles:
            await interaction.followup.send(f"Role must be a watchalong role, list: {list(guildData.roles.keys())}", ephemeral=True)
            return
    
    perms = discord.Permissions(send_messages=True, read_messages=True)
    role = await interaction.guild.create_role(
        name=role_name, 
        colour=discord.Colour.blue(), 
        permissions=perms,
        mentionable=True,
        hoist=False
    )
    guildData.roles[role_name] = RoleClass(
        role_id=role.id,
        day=request_data.day,
        time=request_data.time,
        ping_notice=ping_notice,
        location=location,
        ep_progress=ep_progress,
        total_eps=total_eps,
        ep_rate=ep_rate,
        update_mal=update_mal,
    )

    guildData.reaction_map[react_emoji] = role.id

    day_str = None
    time_str = None
    if(request_data.day is not None and request_data.time is not None and ping_notice is not None):
        day_str = day_names[guildData.roles[role_name].day]
        dt_obj = dt.time.fromisoformat(guildData.roles[role_name].time)
        time_str = dt_obj.strftime("%I:%M %p")
    del guildData.role_queue[role_name]

    if cont_role is not None:
        for member in cont_role.members:
            await member.add_roles(role)

    await update_role_message(interaction.guild.id)
    await bot.save_data()

    message = (
        f"**New Role Added, requested by <@{request_data.requester_id}>**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ep_progress is not None:
        message += f"Episode progress `{guildData.roles[role_name].ep_progress}`"
        if total_eps is not None:
            message += f" out of `{guildData.roles[role_name].total_eps}` episodes"
        if ep_rate is not None:
            message += f" watching `{guildData.roles[role_name].ep_rate}` per meeting"
        message += "\n"
    if day_str or time_str:
        message += f"**Time:** Every `{day_str}` at `{time_str}` in `{TIME_ZONE}`\n"
        if ping_notice is not None:
            message += f"**Ping:** `{guildData.roles[role_name].ping_notice}` minutes before meeting\n"
    if location:
        message += f"**Location:** {guildData.roles[role_name].location}\n"

    if continuation is not None:
        message +=f"\n*This is a continuation of {cont_role}"

    if continuation is None:
        role_channel = bot.get_channel(guildData.role_channel_id)
        if role_channel:
            message += f"Join this watchalong by reacting to {react_emoji} in {role_channel.jump_url}"
        ping_channel = bot.get_channel(guildData.ping_channel_id)
        await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))
        await ping_channel.send(message, allowed_mentions=discord.AllowedMentions(users=False))
    else:
        await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))


@bot.tree.command(name="rmq", description="Remove request from queue")
@app_commands.describe(role_name="Leave blank to deny the most recent request")
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=queued_roles_autocomplete)
async def rmq(interaction: discord.Interaction, role_name: str = None, ):
    await interaction.response.defer()
    guildData = bot.data.guilds[interaction.guild.id]
    if(not role_name and guildData.role_queue):
        role_name = list(guildData.role_queue.keys())[-1]
    if(role_name in guildData.role_queue):
        del guildData.role_queue[role_name]
        await bot.save_data()
        await interaction.followup.send(f"Successfully removed {role_name}", ephemeral=True)
    else:
        await interaction.followup.send(f"No request by that name, list:\n{list(guildData.role_queue.keys())}", ephemeral=True)

@bot.tree.command(name="listq", description="Displays request queue")
@app_commands.default_permissions(manage_roles=True)
async def listq(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(f"list queue:\n{bot.data.guilds[interaction.guild.id].role_queue}", ephemeral=True)

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
    ep_rate="Number of episodes watching per meeting, defaults to 1",
    continuation="Role name if it is a continuation of an existing role, defaults to none",
    update_mal="Set True if updates should be reflected in mal",
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=anilist_search_autocomplete)
@app_commands.autocomplete(continuation=watchalong_roles_autocomplete)
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
    continuation: str = None,
    update_mal: bool = True,
):
    await interaction.response.defer()

    guildData = bot.data.guilds[interaction.guild.id]
    if not role_name:
        await interaction.followup.send("No role name detected", ephemeral=True)
        return
    if(role_name in guildData.roles):
        await interaction.followup.send(f"Role {role_name} already exists: {guildData.roles[role_name]}", ephemeral=True)
        return
    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
    if existing_role:
        await interaction.followup.send(f"❌ A role named `{role_name}` already exists in this server", ephemeral=True)
        return
    if len(guildData.roles) >= 20:
        await interaction.followup.send("❌ The role menu is full! (Discord limits messages to 20 reactions). Please remove old roles first.", ephemeral=True)
        return

    cont_role = None
    if continuation is not None:
        cont_role = discord.utils.get(interaction.guild.roles, name=continuation)
        if continuation not in guildData.roles:
            await interaction.followup.send(f"Role must be a watchalong role, list: {list(guildData.roles.keys())}", ephemeral=True)
            return
    
    if "|eps:" in role_name:
        actual_role_name, eps_string = role_name.rsplit("|eps:", 1)
        role_name = actual_role_name.strip()
        
        if eps_string != "Unknown" and eps_string.isdigit():
            total_eps = int(eps_string)
    else:
        match = re.search(r'\s*\(([0-9]+|Unknown)\s*Eps\)$', role_name, flags=re.IGNORECASE)
        if match:
            eps_string = match.group(1)
            role_name = re.sub(r'\s*\(([0-9]+|Unknown)\s*Eps\)$', '', role_name, flags=re.IGNORECASE).strip()
            
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
    guildData.roles[role_name] = RoleClass(
        role_id=role.id,
        day=day_int,
        time=parsed_time.isoformat() if parsed_time else None,
        ping_notice=ping_notice,
        location=location,
        ep_progress=ep_progress,
        total_eps=total_eps,
        ep_rate=ep_rate,
        update_mal=update_mal,
    )

    guildData.reaction_map[react_emoji] = role.id

    if cont_role is not None:
        for member in cont_role.members:
            await member.add_roles(role)
    await update_role_message(interaction.guild.id)
    await bot.save_data()

    day_str = None
    time_str = None
    if day_int is not None:
        global day_names
        day_str = day_names[guildData.roles[role_name].day]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")

    message = (
        f"**<@{interaction.user.id}> created New Role**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ep_progress is not None:
        message += f"Starting episode progress `{guildData.roles[role_name].ep_progress}`"
        if total_eps is not None:
            message += f" out of `{guildData.roles[role_name].total_eps}` episodes"
        if ep_rate is not None:
            message += f" watching `{guildData.roles[role_name].ep_rate}` per meeting"
        message += "\n"
    if day_str and time_str:
        message += f"**Time:** Every `{day_str}` at `{time_str}` in `{TIME_ZONE}`\n"
        if ping_notice is not None:
            message += f"**Ping:** `{guildData.roles[role_name].ping_notice}` minutes before meeting\n"
    if location:
        message += f"**Location:** {guildData.roles[role_name].location}\n"

    if continuation is not None:
        message +=f"\n*This is a continuation of {cont_role}"

    if continuation is None:
        role_channel = bot.get_channel(guildData.role_channel_id)
        if role_channel:
            message += f"Join this watchalong by reacting to {react_emoji} in {role_channel.jump_url}"
        ping_channel = bot.get_channel(guildData.ping_channel_id)
        await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))
        await ping_channel.send(message, allowed_mentions=discord.AllowedMentions(users=False))
    else:
        await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.command(name="rm", description="Removes a watchalong role")
@app_commands.describe(
    role_name="Name of target role"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.autocomplete(role_name=watchalong_roles_autocomplete)
async def rm(interaction: discord.Interaction, role_name: str):
    await interaction.response.defer()

    guildData = bot.data.guilds[interaction.guild.id]
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    if role_name not in guildData.roles:
        await interaction.followup.send(f"Role must be a watchalong role, list: {list(guildData.roles.keys())}", ephemeral=True)
        return
    del guildData.roles[role_name]
    if role_name in ping_tracker:
        del ping_tracker[role_name]
    if role is None:
        await interaction.followup.send(f"[Warning] Role is not in server, but {role_name} was deleted", ephemeral=True)
        return
    key_to_del = next((k for k, v in guildData.reaction_map.items() if v == role.id), None)
    await role.delete(reason=f"Deleted by {interaction.user.name}")
    if key_to_del:
        del guildData.reaction_map[key_to_del]
        role_channel = bot.get_channel(int(guildData.role_channel_id))
        if role_channel:
            try:
                try:
                    role_message = await role_channel.fetch_message(guildData.react_message_id)
                except Exception as e:
                    await init_react_message(interaction.guild.id)
                    role_message = await role_channel.fetch_message(guildData.react_message_id)
                await role_message.clear_reaction(key_to_del)
            except Exception as e:
                print(f"[WARNING] Could not clear reactions for {key_to_del}: {e}")

        await update_role_message(interaction.guild.id)
        await bot.save_data()
    await interaction.followup.send(f"The role {role.name} has been deleted by <@{interaction.user.id}>.", allowed_mentions=discord.AllowedMentions(users=False))

@bot.tree.command(name="list", description="Displays All Watchalong Roles")
@app_commands.default_permissions(manage_roles=True)
async def listroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    roles = bot.data.guilds[interaction.guild.id].roles
    
    if not roles:
        await interaction.followup.send("No watchalong roles currently active.", ephemeral=True)
        return
        
    role_list_str = "\n".join([f"• **{name}**: {data.ep_progress}/{data.total_eps} eps" for name, data in roles.items()])
    
    if len(role_list_str) > 1900:
        role_list_str = role_list_str[:1900] + "\n... (List truncated)"
        
    await interaction.followup.send(f"**Role List:**\n{role_list_str}", ephemeral=True)

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
    ep_rate="Number of episodes watching per meeting",
    update_mal="Set True if updates should be reflected in mal",
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
    update_mal: bool = None,
):
    await interaction.response.defer()

    guildData = bot.data.guilds[interaction.guild.id]
    if not role_name:
        await interaction.followup.send("No role name detected", ephemeral=True)
        return
    if role_name not in guildData.roles:
        available_roles = list(guildData.roles.keys())
        await interaction.followup.send(f"Role {role_name} not found, existing list: {available_roles}", ephemeral=True)
        return
    role_name = role_name.strip()
    old_ep_progress = guildData.roles[role_name].ep_progress
    old_total_eps = guildData.roles[role_name].total_eps
    old_ep_rate = guildData.roles[role_name].ep_rate
    old_day = guildData.roles[role_name].day
    old_time = guildData.roles[role_name].time
    old_ping_notice = guildData.roles[role_name].ping_notice
    old_location = guildData.roles[role_name].location
    old_update_mal = guildData.roles[role_name].update_mal
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

    if day_int is not None: guildData.roles[role_name].day = day_int
    if time is not None: guildData.roles[role_name].time = parsed_time.isoformat()
    if ping_notice is not None: 
        if ping_notice < 0:
            guildData.roles[role_name].ping_notice = None
        else:
            guildData.roles[role_name].ping_notice = ping_notice
    if location is not None: guildData.roles[role_name].location = location
    if ep_progress is not None: guildData.roles[role_name].ep_progress = ep_progress
    if total_eps is not None: guildData.roles[role_name].total_eps = total_eps
    if ep_rate is not None: guildData.roles[role_name].ep_rate = ep_rate
    if update_mal is not None: guildData.roles[role_name].update_mal = update_mal

    day_str = None
    time_str = None
    if guildData.roles[role_name].day is not None:
        day_str = day_names[guildData.roles[role_name].day]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")
    elif guildData.roles[role_name].time:
        time_obj = dt.time.fromisoformat(guildData.roles[role_name].time)
        time_str = time_obj.strftime("%I:%M %p")

    message = (
        f"**<@{interaction.user.id}> updated role: `{role_name}`**\n"
    )

    if react_emoji is not None:
        if(not emoji.is_emoji(react_emoji)):
            await interaction.followup.send(f"Is not valid emoji", ephemeral=True)
        else:
            old_emoji = next((k for k, v in guildData.reaction_map.items() if v == guildData.roles[role_name].role_id), None)
            if old_emoji is None:
                print(f"[ERROR] No pair in reaction_map with role {role_name}, {guildData.reaction_map}")
                return
            await move_reacts(guildData, old_emoji, react_emoji)
            guildData.reaction_map[react_emoji] = guildData.roles[role_name].role_id
            del guildData.reaction_map[old_emoji]
            message += f"Changed Reaction Emoji from {old_emoji}->{react_emoji}"

    await update_role_message(interaction.guild.id)
    await bot.save_data()

    if ep_progress is not None and (ep_progress != old_ep_progress):
        message += f"**Current episode progress** `{old_ep_progress}`->`{guildData.roles[role_name].ep_progress}`\n"
    else:
        message += f"**Current episode progress** `{guildData.roles[role_name].ep_progress}`\n"
    if total_eps is not None and (total_eps != old_total_eps):
        message += f"**Total episodes** `{old_total_eps}`->`{guildData.roles[role_name].total_eps}`\n"
    else:
        message += f"**Total episodes** `{guildData.roles[role_name].total_eps}`\n"
    if ep_rate is not None and (ep_rate != old_ep_rate):
        message += f"**Episode rate** `{old_ep_rate}`->`{guildData.roles[role_name].ep_rate}` per meeting\n"
    else:
        message += f"**Episode rate** `{guildData.roles[role_name].ep_rate} per meeting`\n"
    message += f"**Time:** Every "
    if(day is not None): message += f"`{old_day_str}` -> "
    message += f"`{day_str}` at "
    if(time is not None): message += f"`{old_time_str}` -> "
    message += f"`{time_str}` in `{TIME_ZONE}`\n"
    if ping_notice is not None and (old_ping_notice != ping_notice):
        message += f"**Ping:** `{old_ping_notice}` -> `{guildData.roles[role_name].ping_notice}` minutes before meeting\n"
    else:
        message += f"**Ping:** `{guildData.roles[role_name].ping_notice}` minutes before meeting\n"
    if location is not None and (location != old_location):
        message += f"**Location:** {old_location} -> {guildData.roles[role_name].location}\n"
    else:
        message += f"**Location:** {guildData.roles[role_name].location}\n"
    if(update_mal is not None): message += f"Update MAL: `{old_update_mal}` -> `{update_mal}`"
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

    guildData = bot.data.guilds[interaction.guild.id]
    if role_name not in guildData.roles:
        available_roles = list(guildData.roles.keys())
        await interaction.followup.send(f"Warning, no role {role_name} in list: {available_roles}", ephemeral=True)
    now = dt.datetime.now(zoneinfo.ZoneInfo(TIME_ZONE))

    role_data = guildData.roles[role_name]
    if role_data.day is None or role_data.time is None:
        await interaction.followup.send(f"Warning, no day/time set for {role_name}: {role_data}", ephemeral=True)
        return
    time_obj = dt.time.fromisoformat(role_data.time)
    temp_days = role_data.day-now.weekday()
    if temp_days < 0 or (temp_days == 0 and now.time() > time_obj): temp_days+=7
    target_date = now + timedelta(days=temp_days)
    dt_obj = dt.datetime.combine(target_date, time_obj)
    if role_data.ping_notice is not None:
        target_dt_obj = dt_obj - timedelta(minutes=role_data.ping_notice)
    else:
        target_dt_obj = dt_obj
    ping_tracker[role_name] = target_dt_obj

    formatted = target_dt_obj.strftime("%A %I:%M %p")
    await interaction.followup.send(f"<@{interaction.user.id}> skipping planned ping `{formatted}`", allowed_mentions=discord.AllowedMentions(users=False))

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
    if payload.message_id != bot.data.guilds[payload.guild_id].react_message_id:
        return
    if str(payload.emoji) in bot.data.guilds[payload.guild_id].reaction_map:
        guild = bot.get_guild(payload.guild_id)
        role_id = bot.data.guilds[payload.guild_id].reaction_map[str(payload.emoji)]
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
    if payload.message_id != bot.data.guilds[payload.guild_id].react_message_id:
        return

    if str(payload.emoji) in bot.data.guilds[payload.guild_id].reaction_map:
        guild = bot.get_guild(payload.guild_id)
        role_id = bot.data.guilds[payload.guild_id].reaction_map[str(payload.emoji)]
        role = guild.get_role(role_id)

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            member = None
        if role and member:
            await member.remove_roles(role)
        else:
            print(f"[ERROR] Could not find role and/or member from role id: {role_id} member_id {payload.user_id} for reaction: {payload.emoji}")

async def update_role_message(guild_id: int):
    guildData = bot.data.guilds[guild_id]
    if not guildData or not guildData.role_channel_id:
        return
    
    channel = bot.get_channel(guildData.role_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(guildData.role_channel_id)
        except Exception:
            print(f"[Warning] Could not fetch role channel {guildData.role_channel_id} for guild {guild_id}")
            return

    if not guildData.react_message_id:
        await init_react_message(guild_id)
        guildData = bot.data.guilds.get(guild_id)
        if not guildData or not guildData.react_message_id:
            return
    
    try:
        msg = await channel.fetch_message(guildData.react_message_id)
    except Exception as e:
        await init_react_message(guild_id)
        msg = await channel.fetch_message(guildData.react_message_id)
    
    sorted_roles = []

    for emoji, role_id in guildData.reaction_map.items():
        role_name = None
        role_info = None

        for name, info in guildData.roles.items():
            info_role_id = getattr(info, 'role_id', None) if not isinstance(info, dict) else info.get('role_id')
            if info_role_id == role_id:
                role_name = name
                role_info = info
                break

        if not role_info:
            role = channel.guild.get_role(role_id)
            if role:
                role_name = role.name
                role_info = guildData.roles.get(role.name)

        if not role_info or not role_name:
            continue

        sort_day = role_info.day if role_info.day is not None else 7
        sort_time = role_info.time if role_info.time is not None else "23:59:59"

        sorted_roles.append((sort_day, sort_time, emoji, role_name, role_info))

    sorted_roles.sort(key=lambda x: (x[0], x[1]))

    message = (
        f"**Role Menu: Anime Watchalongs**\n"
        f"React to give yourself a role.\n"
        f"Members can use /rq to request an anime as a watchalong to be approved by an admin\n"
    )
    global day_names
    for sort_day, sort_time, emoji, role_name, role_info in sorted_roles:
        message += (f"\n{emoji} : `{role_name}`")
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

    for emoji_str in guildData.reaction_map.keys():
        if emoji_str not in bot_reactions:
            try:
                await msg.add_reaction(emoji_str)
            except Exception as e:
                print(f"[WARNING] Could not add reaction {emoji_str}: {e}")

async def init_react_message(guild_id: int):
    guildData = bot.data.guilds.get(guild_id)
    if not guildData or not guildData.role_channel_id:
        return
    
    channel = bot.get_channel(bot.data.guilds[guild_id].role_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(guildData.role_channel_id)
        except Exception:
            print(f"[Warning] Could not fetch role channel {guildData.role_channel_id} for guild {guild_id}")
            return
        
    message = await channel.send(f"**Role Menu: Anime Watchalongs**\n"
                                 f"React to give yourself a role.\n"
                                 f"Members can use /rq to request an anime as a watchalong to be approved by an admin\n")
    bot.data.guilds[guild_id].react_message_id = message.id
    await bot.save_data()


@bot.tree.command(name="setup", description="Setup channels for bot")
@app_commands.describe(
    role_channel="channel where you want the role message where people react to get roles",
    ping_channel="channel where you want the ping for roles",
    ticket_channel="channel where you want member request messages to print to"
)
@app_commands.default_permissions(administrator=True)
async def setup_channels(interaction: discord.Interaction, role_channel: discord.TextChannel, ping_channel: discord.TextChannel, ticket_channel: discord.TextChannel):
    guild_id = interaction.guild.id

    if guild_id not in bot.data.guilds:
        bot.data.guilds[guild_id] = GuildData(
            role_queue={},
            roles={},
            reaction_map={}
        )
    
    bot.data.guilds[guild_id].role_channel_id = role_channel.id
    bot.data.guilds[guild_id].ping_channel_id = ping_channel.id
    bot.data.guilds[guild_id].ticket_channel_id = ticket_channel.id
    
    await interaction.response.send_message(f"Role menu channel set to {role_channel.mention}, ping channel set to {ping_channel.mention}, and ticket channel set to {ticket_channel.mention}", ephemeral=True)
    await bot.save_data()
    await init_react_message(guild_id)

async def move_reacts(guildData: GuildData, old_emoji, new_emoji):
    channel = bot.get_channel(guildData.role_channel_id)
    try:
        message = await channel.fetch_message(guildData.react_message_id)
    except discord.NotFound:
        await init_react_message(channel.guild.id)
        message = await channel.fetch_message(guildData.react_message_id)

    old_reaction = discord.utils.get(message.reactions, emoji=old_emoji)
    if not old_reaction:
        print("[ERROR] Old emoji reaction not found on this message.")
        return

    await message.add_reaction(new_emoji)

    await message.clear_reaction(old_emoji)

bot.run(BOT_TOKEN)