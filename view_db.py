import sqlite3

conn = sqlite3.connect("bot.db")
conn.row_factory = sqlite3.Row

cur = conn.execute("SELECT * FROM users LIMIT 20")
print("USERS:")
for row in cur:
    print(dict(row))

print("\nBOT BALANCE:")
cur = conn.execute("SELECT * FROM bot_balance")
for row in cur:
    print(dict(row))

print("\nPAYMENTS (последние 10):")
cur = conn.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 10")
for row in cur:
    print(dict(row))

conn.close()
