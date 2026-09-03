const DEFAULT_SETTINGS = {
  targetMargin: 0.30,
  defaultCommission: 0.18,
};

chrome.runtime.onInstalled.addListener(async () => {
  const { settings } = await chrome.storage.local.get("settings");
  if (!settings) {
    await chrome.storage.local.set({ settings: DEFAULT_SETTINGS });
  }
});

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;
  try {
    await chrome.sidePanel.setOptions({ tabId: tab.id, path: "panel.html", enabled: true });
    await chrome.sidePanel.open({ tabId: tab.id });
  } catch (error) {
    console.warn("无法打开侧边面板：", error);
  }
});
