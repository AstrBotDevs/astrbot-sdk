use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let plugin_root = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    let worker = astrbot_plugin_template::build_worker(plugin_root)?;
    worker.run_stdio().await?;
    Ok(())
}
