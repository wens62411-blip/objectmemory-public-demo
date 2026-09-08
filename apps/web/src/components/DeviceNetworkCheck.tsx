import { RefreshCw, Wifi } from 'lucide-react'
import { useEffect } from 'react'
import { useApi } from '../hooks/useApi'

interface NetworkCheck {
  status: string; checked_at?: string; reason?: string; message?: string
  connections: Array<{ ssid: string; band: string | null; channel: number | null; observed_2_4ghz?: boolean }>
}

export function DeviceNetworkCheck({ mode, ssid, onDetectedSsid }: { mode: string; ssid: string; onDetectedSsid?: (ssid: string) => void }) {
  const scan = useApi<NetworkCheck>(mode === 'REAL' ? '/api/firmware/network-status' : null,
    { status: 'unavailable', connections: [] })
  const uniqueSsid = scan.data.connections?.length === 1 ? scan.data.connections[0].ssid : undefined
  useEffect(() => {
    if (mode === 'REAL' && uniqueSsid && !ssid) onDetectedSsid?.(uniqueSsid)
  }, [mode, uniqueSsid, ssid, onDetectedSsid])
  if (mode !== 'REAL') return null
  const connections = scan.data.connections ?? []
  const matched = connections.find((row) => row.ssid === ssid)
  const mismatch = Boolean(ssid && connections.length && !matched)
  const needsBand = connections.some((row) => !row.observed_2_4ghz)
  return <section className="fallback-provision" aria-label="热点兼容性检查">
    <div className="section-heading"><h3><Wifi aria-hidden="true" />热点兼容性</h3>
      <button className="button secondary small" type="button" disabled={scan.loading} onClick={() => void scan.reload()}>
        <RefreshCw aria-hidden="true" />{scan.loading ? '读取中…' : '重新检查网络'}
      </button>
    </div>
    <p>USB-C 数据线负责安装和配网；视频通过 2.4 GHz Wi-Fi 传回电脑。USB 不会自动共享电脑网络。</p>
    {connections.map((row) => <p key={row.ssid}>电脑当前连接：<strong>{row.ssid}</strong> · {row.band ?? '频段未知'}{row.channel != null && ` · 信道 ${row.channel}`}</p>)}
    {!scan.loading && !connections.length && <p role="status">尚未读到电脑的 Wi-Fi 连接；有线连接也可使用，但仍需确认板卡热点有 2.4 GHz。</p>}
    {!scan.loading && !ssid && <p role="status">未能唯一确定 Wi-Fi 名称，请在上方填写板卡要连接的 2.4 GHz 热点名称；只有密码无法确定网络。</p>}
    {mismatch && <p className="danger-text" role="alert">填写的 Wi-Fi 名称与电脑当前连接不同，请核对大小写和拼写；若有意使用另一网络，请确认它与电脑互通。</p>}
    {needsBand && <p className="danger-text" role="status">没有观测到当前热点的 2.4 GHz 信号。请检查手机“个人热点”的 AP 频段，修改后关闭再开启热点，再重新检查。双频路由器可能只显示其中一个频段，不能据此认定板卡已连接或一定不能连接。</p>}
    {connections.length > 0 && !needsBand && <p role="status">已观测到 2.4 GHz；板卡是否入网、电脑是否可达，还需后续真实配网验证。</p>}
    {(scan.error || scan.data.reason) && <p className="danger-text" role="status">{scan.error || scan.data.reason}</p>}
    <small>只读取电脑网络，不读取保存的 Wi-Fi 密码；检查结果最多缓存 10 秒。{scan.data.checked_at && `上次检查：${new Date(scan.data.checked_at).toLocaleTimeString()}`}</small>
  </section>
}
