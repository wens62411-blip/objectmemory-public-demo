import { useApi } from '../hooks/useApi'
import { ErrorState } from './UI'

interface BoardCapability {
  board_model: string; label: string; chip: string; connection: string
  source_present: boolean; build_verified: boolean
}
interface CapabilityResponse { boards: BoardCapability[]; notice?: string }

/** Capability is not a connected-device or completed-flash claim. */
export function FirmwareCompatibility() {
  const result = useApi<CapabilityResponse>('/api/firmware/boards', { boards: [] })
  const boards = Array.isArray(result.data.boards) ? result.data.boards : []
  return <details className="fallback-provision">
    <summary>支持哪些开发板？查看适配与验证状态</summary>
    {result.error ? <ErrorState message="板型能力读取失败；不会假定通用兼容。" onRetry={result.reload} /> : <>
      <p>{result.data.notice || '正在读取后台已实现的专用板型。'}</p>
      <ul>{boards.map(board => <li key={board.board_model}>
        <strong>{board.label}</strong> · {board.chip}<br />
        {board.connection}<br />
        {board.source_present ? '专用源码与受控读写流程已实现' : '本地专用源码不完整'}；
        {board.build_verified ? '当前编译产物哈希已验证' : '当前编译产物尚未验证'}。
      </li>)}</ul>
      <p>此表不代表已接入或刷写了这些板。当前设备的固件启动、Wi-Fi、后台注册和真实视频，以逐项日志为准。其他 ESP32 / ESP32-S3、无摄像头扩展的 XIAO 和消费级相机不自动套用以上固件。</p>
    </>}
  </details>
}
