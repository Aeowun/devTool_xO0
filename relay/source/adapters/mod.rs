use anyhow::Result;
use async_trait::async_trait;

#[derive(Debug, Clone, PartialEq)]
pub enum Participant {
    Gemini,
    ChatGPT,
}

#[async_trait]
pub trait Adapter {
    async fn connect(&mut self) -> Result<()>;
    async fn find_conversation(&mut self) -> Result<()>;
    async fn read_latest_message(&mut self) -> Result<String>;
    async fn send_message(&mut self, text: &str) -> Result<()>;
    async fn wait_for_response(&mut self) -> Result<()>;
    async fn is_generating(&mut self) -> Result<bool>;
    async fn disconnect(&mut self) -> Result<()>;
}

pub mod chatgpt;
pub mod gemini;
