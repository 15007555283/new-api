package system_setting

import "github.com/QuantumNous/new-api/setting/config"

type ThemeSettings struct {
	Frontend string `json:"frontend"`
}

var themeSettings = ThemeSettings{
	Frontend: "default",
}

func init() {
	config.GlobalConfig.Register("theme", &themeSettings)
}

func GetThemeSettings() *ThemeSettings {
	return &themeSettings
}

// UpdateAndSyncTheme 保留旧配置加载入口；classic 前端已移除，状态接口固定返回 default。
func UpdateAndSyncTheme() {}
