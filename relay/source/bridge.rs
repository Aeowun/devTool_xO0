use anyhow::Result;
use crate::adapters::{Adapter, Participant};
use crate::db::Database;
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct Bridge {
    gemini: Box<dyn Adapter + Send>,
    chatgpt: Box<dyn Adapter + Send>,
    db: Arc<Database>,
    max_turns: i32,
}

impl Bridge {
    pub fn new(gemini: Box<dyn Adapter + Send>, chatgpt: Box<dyn Adapter + Send>, db: Arc<Database>) -> Self {
        Self {
            gemini,
            chatgpt,
            db,
            max_turns: 10,
        }
    }

    pub async fn run(&mut self, session_id: &str) -> Result<()> {
        println!("Starting Relay Session: {}", session_id);

        self.gemini.connect().await?;
        self.chatgpt.connect().await?;

        let mut current_turn = 0;

        loop {
            if current_turn >= self.max_turns {
                println!("Max turns reached.");
                break;
            }

            // Turn: Gemini -> ChatGPT
            println!("[Gemini -> ChatGPT] Processing...");
            let g_msg = self.gemini.read_latest_message().await?;
            self.db.store_message(session_id, current_turn, "GEMINI", &g_msg).await?;

            self.chatgpt.send_message(&g_msg).await?;
            self.chatgpt.wait_for_response().await?;

            // Turn: ChatGPT -> Gemini
            println!("[ChatGPT -> Gemini] Processing...");
            let c_msg = self.chatgpt.read_latest_message().await?;
            self.db.store_message(session_id, current_turn, "CHATGPT", &c_msg).await?;

            self.gemini.send_message(&c_msg).await?;
            self.gemini.wait_for_response().await?;

            current_turn += 1;
        }

        Ok(())
    }
}
