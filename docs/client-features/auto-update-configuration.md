# Auto Update Configuration

  auto_update_configuration:
    description: "Automatic package updates can be enabled or disabled without changing the environment."
    configuration:
      file: "~/.auto-coder/config.toml or .auto-coder/config.toml"
      section: "[auto_update]"
      option: "enabled = true or false"
      default: true
    behavior:
      - "When enabled, pipx and uv tool installations continue to check for and apply updates at the configured interval."
      - "When disabled, update checks and automatic restarts are skipped."
      - "AUTO_CODER_DISABLE_AUTO_UPDATE remains available as an environment-level force-disable override."
