import datetime
import zoneinfo
import os

def parse_schedule(day_str: str, time_str: str):
    """
    Takes user input strings and returns a tuple: (day_integer, datetime.time_object)
    Returns (None, None) if the input is completely invalid.
    """
    # 1. Parse the Day (0 = Monday, 6 = Sunday)
    day_mapping = {
        'mon': 0, 'monday': 0,
        'tue': 1, 'tues': 1, 'tuesday': 1,
        'wed': 2, 'wednesday': 2,
        'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3,
        'fri': 4, 'friday': 4,
        'sat': 5, 'saturday': 5,
        'sun': 6, 'sunday': 6
    }
    if(day_str):
        clean_day = day_str.lower().strip()
        day_int = day_mapping.get(clean_day)
        if(day_int is None):
            day_int = -1
    else:
        day_int = None

    # 2. Parse the Time
    time_formats = [
        "%H:%M",       # 14:30 or 07:00
        "%I:%M %p",    # 2:30 PM or 07:00 AM
        "%I:%M%p",     # 2:30PM (no space)
        "%I %p",       # 2 PM (no minutes)
        "%I%p",        # 2PM (no minutes, no space)
    ]
    

    parsed_time = None
    if(time_str):
        parsed_time = "err"
        clean_time = time_str.strip()
        
        for fmt in time_formats:
            try:
                parsed_time = datetime.datetime.strptime(clean_time, fmt).time()
                break
            except ValueError:
                continue

    return day_int, parsed_time

DEFAULT_EMOJI_POOL = [
    "🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫", "⚪",
    "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬛", "⬜",
    "🍎", "🍊", "🍋", "🍉", "🍇", "🫐", "🥝", "🥥", "🍍"
]

def get_available_emoji(bot):
    """Finds the first emoji in the pool that isn't currently being used."""
    for emoji in DEFAULT_EMOJI_POOL:
        if emoji not in bot.data.reaction_map:
            return emoji
    return None

def get_datetime(role_data, now=None) -> datetime:
    if now is None:
        now = datetime.datetime.now(zoneinfo.ZoneInfo(os.getenv("TIME_ZONE")))
    time_obj = datetime.time.fromisoformat(role_data.time)
    temp_days = role_data.day-now.weekday()
    if temp_days < 0 or (temp_days == 0 and now.replace(microsecond=0).time() > time_obj): temp_days+=7
    target_date = now + datetime.timedelta(days=temp_days)
    target_dt_obj = datetime.datetime.combine(target_date, time_obj)
    if(role_data.ping_notice is not None):
        target_dt_obj = target_dt_obj - datetime.timedelta(minutes=role_data.ping_notice)
    return target_dt_obj

def compare_weekday(date: datetime, now: datetime = None) -> bool:
     if now is None:
        now = datetime.datetime.now(zoneinfo.ZoneInfo(os.getenv("TIME_ZONE")))
     return now.day == date.day and now.hour == date.hour and now.minute == date.minute

def check_ping_tracker(last_ping: datetime, date: datetime) -> bool:
    return (last_ping is not None and (last_ping.second == date.second and
            last_ping.minute == date.minute and
            last_ping.hour == date.hour and
            last_ping.day == date.day and
            last_ping.month == date.month and
            last_ping.year == date.year))