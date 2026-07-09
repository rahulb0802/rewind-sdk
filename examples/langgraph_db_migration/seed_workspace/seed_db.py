import sqlite3

conn = sqlite3.connect("shop.db")
cur = conn.cursor()
cur.execute("DROP TABLE IF EXISTS users")
cur.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, credits INTEGER)")

rows = [
    (1, "a@x.com", 40),
    (2, "b@x.com", 99),
    (3, "c@x.com", 100),
    (4, "d@x.com", 250),
    (5, "e@x.com", 499),
    (6, "f@x.com", 500),
    (7, "g@x.com", 800),
    (8, "h@x.com", 12),
    (9, "i@x.com", 365),
    (10, "j@x.com", 999),
]
cur.executemany("INSERT INTO users VALUES (?,?,?)", rows)
conn.commit()
conn.close()
print(f"seeded {len(rows)} users")