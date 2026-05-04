import sqlite3
import os

def scan_all_dbs():
    base_dir = "/home/ubuntu"
    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file.endswith(".db"):
                db_path = os.path.join(root, file)
                try:
                    conn = sqlite3.connect(db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
                    tables = [t[0] for t in cur.fetchall()]
                    
                    print(f"\nDB: {db_path}")
                    for table in tables:
                        try:
                            cur.execute(f"SELECT COUNT(*) FROM {table}")
                            count = cur.fetchone()[0]
                            print(f"  Table: {table:<20} | Count: {count}")
                        except:
                            print(f"  Table: {table:<20} | (Error counting)")
                    conn.close()
                except:
                    pass

if __name__ == "__main__":
    scan_all_dbs()
