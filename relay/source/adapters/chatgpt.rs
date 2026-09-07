use anyhow::{Result, anyhow};
use async_trait::async_trait;
use super::Adapter;
use headless_chrome::{Browser, LaunchOptions};
use std::sync::Arc;

pub struct ChatGPTAdapter {
    browser: Option<Browser>,
    tab: Option<Arc<headless_chrome::Tab>>,
}

impl ChatGPTAdapter {
    pub fn new() -> Self {
        Self {
            browser: None,
            tab: None,
        }
    }
}

#[async_trait]
impl Adapter for ChatGPTAdapter {
    async fn connect(&mut self) -> Result<()> {
        let options = LaunchOptions::default_builder()
            .headless(false) // We want to see it for experimentation
            .path(None) // Use default chrome path
            .build()?;

        let browser = Browser::new(options)?;
        self.browser = Some(browser);

        let tab = self.browser.as_ref().unwrap().wait_for_initial_tab()?;
        tab.navigate_to("https://chatgpt.com")?;
        tab.wait_until_navigated()?;

        self.tab = Some(tab);
        Ok(())
    }

    async fn find_conversation(&mut self) -> Result<()> {
        // logic to ensure we are in a chat
        Ok(())
    }

    async fn read_latest_message(&mut self) -> Result<String> {
        let tab = self.tab.as_ref().ok_or(anyhow!("Not connected"))?;
        // Find latest assistant message
        let element = tab.wait_for_element("div.agent-turn")?;
        let text = element.get_inner_text()?;
        Ok(text)
    }

    async fn send_message(&mut self, text: &str) -> Result<()> {
        let tab = self.tab.as_ref().ok_or(anyhow!("Not connected"))?;
        let textarea = tab.wait_for_element("textarea#prompt-textarea")?;
        textarea.click()?;
        textarea.type_str(text)?;
        textarea.press_key("Enter")?;
        Ok(())
    }

    async fn wait_for_response(&mut self) -> Result<()> {
        let tab = self.tab.as_ref().ok_or(anyhow!("Not connected"))?;
        // Poll for stop button to disappear
        loop {
            if !self.is_generating().await? {
                break;
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
        }
        Ok(())
    }

    async fn is_generating(&mut self) -> Result<bool> {
        let tab = self.tab.as_ref().ok_or(anyhow!("Not connected"))?;
        let stop_button = tab.find_element("button[data-testid=\"stop-button\"]");
        Ok(stop_button.is_ok())
    }

    async fn disconnect(&mut self) -> Result<()> {
        self.browser = None;
        self.tab = None;
        Ok(())
    }
}
