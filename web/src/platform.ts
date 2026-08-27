export interface PlatformBridge {
  copyText(value: string): Promise<void>;
  notify(title: string, body: string): Promise<void>;
  openExternal(url: string): void;
}

export const browserBridge: PlatformBridge = {
  async copyText(value) {
    await navigator.clipboard.writeText(value);
  },
  async notify(title, body) {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") await Notification.requestPermission();
    if (Notification.permission === "granted") new Notification(title, { body });
  },
  openExternal(url) {
    window.open(url, "_blank", "noopener,noreferrer");
  },
};
