use astrbot_plugin_template::TemplateConfig;
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let package_root = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    let out = astrbot_worker::export_config_schema::<TemplateConfig>(package_root)?;
    println!("exported config schema to {}", out.display());
    Ok(())
}
