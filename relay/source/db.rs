use sqlx::{SqlitePool, sqlite::SqlitePoolOptions};
use anyhow::Result;

pub struct Database {
    pool: SqlitePool,
}

impl Database {
    pub async fn new(url: &str) -> Result<Self> {
        let pool = SqlitePoolOptions::new()
            .connect(url).await?;

        sqlx::query("CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            turn_id INTEGER,
            sender TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT
        )").execute(&pool).await?;

        Ok(Self { pool })
    }

    pub async fn store_message(&self, session_id: &str, turn_id: i32, sender: &str, content: &str) -> Result<()> {
        sqlx::query("INSERT INTO messages (session_id, turn_id, sender, content, status) VALUES (?, ?, ?, ?, ?)")
            .bind(session_id)
            .bind(turn_id)
            .bind(sender)
            .bind(content)
            .bind("SENT")
            .execute(&self.pool).await?;
        Ok(())
    }
}
