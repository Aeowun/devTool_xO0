mod adapters;
mod bridge;
mod db;

use anyhow::Result;
use clap::{Parser, Subcommand};
use crate::adapters::chatgpt::ChatGPTAdapter;
use crate::adapters::gemini::GeminiAndroidStudioAdapter;
use crate::bridge::Bridge;
use crate::db::Database;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "relay")]
#[command(about = "Gemini to ChatGPT Relay Utility", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    Start {
        #[arg(short, long, default_value = "default-session")]
        session: String,
    },
    Status,
    History,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Start { session } => {
            let db = Arc::new(Database::new("sqlite:relay.db").await?);
            let gemini = Box::new(GeminiAndroidStudioAdapter::new());
            let chatgpt = Box::new(ChatGPTAdapter::new());

            let mut bridge = Bridge::new(gemini, chatgpt, db);
            bridge.run(&session).await?;
        }
        Commands::Status => {
            println!("Relay Status: IDLE");
        }
        Commands::History => {
            println!("Recent history...");
        }
    }

    Ok(())
}
