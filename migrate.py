import asyncio
import aiosqlite
import json

async def migrate_database():
    GUILD_ID = 220953384763654144   
    TICKET_CHANNEL_ID=1490937551178170448
    ROLE_CHANNEL_ID = 1491144662428156036
    PING_CHANNEL_ID = 1491254484079218709
    
    print(f"Starting migration for Guild: {GUILD_ID}")

    async with aiosqlite.connect("bot_state.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_state (
                guild_id INTEGER,
                key TEXT,
                value TEXT,
                PRIMARY KEY (guild_id, key)
            )
        """)
        
        try:
            async with db.execute("SELECT key, value FROM state") as cursor:
                old_data = await cursor.fetchall()
        except aiosqlite.OperationalError:
            print("Error: The old 'state' table does not exist. Migration already complete or wrong DB.")
            return

        if not old_data:
            print("The old 'state' table is empty. Nothing to migrate.")
            return

        for key, value in old_data:
            await db.execute(
                "REPLACE INTO guild_state (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD_ID, key, value)
            )
            print(f"Migrated: {key}")

        await db.execute(
            "REPLACE INTO guild_state (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD_ID, "role_channel_id", json.dumps(ROLE_CHANNEL_ID))
        )
        print("Injected: role_channel_id")

        await db.execute(
            "REPLACE INTO guild_state (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD_ID, "ping_channel_id", json.dumps(PING_CHANNEL_ID))
        )
        print("Injected: ping_channel_id")

        await db.execute(
            "REPLACE INTO guild_state (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD_ID, "ticket_channel_id", json.dumps(TICKET_CHANNEL_ID))
        )
        print("Injected: ticket_channel_id")
        
        await db.execute("ALTER TABLE state RENAME TO state_backup")

        await db.commit()
        print("\nMigration completed successfully! The old table was renamed to 'state_backup'.")

if __name__ == "__main__":
    asyncio.run(migrate_database())