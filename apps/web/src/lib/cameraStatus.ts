const statusMessages: Record<string, string> = {
  stopped: '已停止',
  probing: '正在探测摄像头',
  ready: '已读取到画面',
  starting: '正在启动摄像头',
  streaming: '实时画面正常',
  reconnecting: '画面中断，正在重新连接',
  busy: '摄像头可能正被其他程序占用，请关闭相机、会议或直播软件后重试',
  permission_denied: 'Windows 摄像头权限已关闭，请打开系统隐私设置中的摄像头访问权限',
  no_device: 'Windows 没有检测到可用摄像头，请检查设备连接和驱动',
  frame_read_failed: '摄像头已经打开，但没有读到画面，请检查占用、驱动或分辨率',
  error: '摄像头发生错误，请查看诊断详情',
  waiting_for_browser: '等待浏览器授权并发送画面',
}

export function cameraStatusText(status?: string, error?: string | null) {
  if (error) return error
  const key = (status || 'stopped').trim().toLowerCase()
  return statusMessages[key] || status || '状态未知'
}

export function browserCameraError(error: unknown) {
  const value = error as DOMException | undefined
  const messages: Record<string, string> = {
    NotAllowedError: '浏览器没有获得摄像头权限。请在地址栏的摄像头图标中允许访问后重试。',
    NotFoundError: '浏览器没有找到摄像头，请检查设备连接和 Windows 隐私设置。',
    NotReadableError: '摄像头可能正被其他程序占用。请自行关闭相机、会议或直播软件后重试。',
    OverconstrainedError: '摄像头不支持所选分辨率或设备，请改用默认设备后重试。',
    SecurityError: '浏览器安全策略阻止了摄像头访问。请使用 localhost 或 HTTPS 打开本页。',
    AbortError: '摄像头启动被系统中断，请稍后重试。',
  }
  return messages[value?.name || ''] || (value?.message ? `浏览器摄像头错误：${value.message}` : '浏览器无法打开摄像头，请查看摄像头诊断。')
}
