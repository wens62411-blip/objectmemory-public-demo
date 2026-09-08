import QRCode from 'qrcode'
import { Check, Copy, ExternalLink, KeyRound, Smartphone, Wifi } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

/** Never put credentials, PINs, arbitrary query data or loopback URLs in a phone QR. */
export function safeMobileUrl(input: string | null | undefined, pathname?: string): string {
  if (!input) return ''
  try {
    const url = new URL(input)
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, '')
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return ''
    if (host === 'localhost' || host.endsWith('.localhost') || host === '::1' || host === '0.0.0.0' || /^127\./.test(host)) return ''
    const octets = host.split('.')
    const ipv4 = octets.length === 4 && octets.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
    const isLan = (ipv4 && (octets[0] === '10' || (octets[0] === '192' && octets[1] === '168') || (octets[0] === '172' && Number(octets[1]) >= 16 && Number(octets[1]) <= 31)))
      || /^(fc|fd)[0-9a-f]{2}:/.test(host) || /^fe[89ab][0-9a-f]:/.test(host) || host.endsWith('.local')
    if (!isLan) return ''
    const permitted = new URLSearchParams()
    if (!pathname && url.pathname === '/companion/validation-marker') {
      for (const key of ['item', 'camera', 'run']) {
        const value = url.searchParams.get(key)
        if (value) permitted.set(key, value)
      }
    }
    url.pathname = pathname || (['/companion', '/companion/validation-marker'].includes(url.pathname) ? url.pathname : '/')
    url.search = permitted.toString()
    url.hash = ''
    return url.toString()
  } catch { return '' }
}

export function PhoneQr({ url, label = '手机打开物忆的二维码' }: { url: string; label?: string }) {
  const qr = useMemo(() => {
    try {
      const modules = QRCode.create(url, { errorCorrectionLevel: 'M' }).modules
      let path = ''
      for (let y = 0; y < modules.size; y += 1) {
        for (let x = 0; x < modules.size; x += 1) {
          if (modules.get(y, x)) path += `M${x + 4} ${y + 4}h1v1h-1z`
        }
      }
      return { size: modules.size + 8, path }
    } catch { return null }
  }, [url])
  if (!qr) return <p role="status">二维码生成失败，请复制下方地址。</p>
  return <svg className="phone-qr" viewBox={`0 0 ${qr.size} ${qr.size}`} role="img" aria-label={label} shapeRendering="crispEdges">
    <rect width={qr.size} height={qr.size} fill="#fff" /><path d={qr.path} fill="#111" stroke="none" />
  </svg>
}

export function MobileAccess({ url, marker = false, compact = false }: { url?: string; marker?: boolean; compact?: boolean }) {
  const safeUrl = safeMobileUrl(url)
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const latestUrl = useRef(safeUrl)
  latestUrl.current = safeUrl
  useEffect(() => { setCopied(false); setCopyError('') }, [safeUrl])

  async function copy() {
    if (!safeUrl) return
    let ok = false
    try {
      if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(safeUrl); ok = true }
    } catch { /* Plain HTTP on a LAN may not provide the Clipboard API. */ }
    if (latestUrl.current !== safeUrl) return
    if (!ok) {
      inputRef.current?.focus(); inputRef.current?.select()
      try { ok = document.execCommand('copy') } catch { ok = false }
    }
    setCopied(ok)
    setCopyError(ok ? '' : '链接已选中，请长按复制，或按 Ctrl+C。')
  }

  return <section className={`mobile-access ${compact ? 'compact' : ''}`} aria-label={marker ? '手机识别标签接入' : '手机接入'}>
    <div className="mobile-access-intro"><span className="mobile-access-symbol"><Smartphone /></span><div><p className="eyebrow">电脑记住 · 手机找到</p><h2>{marker ? '在手机上打开识别标签' : '把物忆带到手机上'}</h2><p>{marker ? '用手机自带相机扫下面的二维码，再让电脑摄像头看见手机上的标签。' : '不用安装 App。在家中连接同一 Wi-Fi，就能查看物品的位置记录。'}</p></div></div>
    {safeUrl ? <div className="mobile-access-content">
      <div className="phone-qr-frame"><PhoneQr url={safeUrl} label={marker ? '用手机打开 Marker 页的二维码' : '手机打开物忆的二维码'} /><span>手机相机扫码打开</span></div>
      <div className="mobile-access-guide">
        <ol className="phone-connect-steps"><li><Wifi /><div><strong>连接同一 Wi-Fi</strong><span>手机和运行物忆的电脑，连接同一个家庭网络。</span></div></li><li><Smartphone /><div><strong>扫码，在浏览器打开</strong><span>使用手机自带相机。微信内打不开时，请选择“在浏览器打开”。</span></div></li><li><KeyRound /><div><strong>输入电脑上的访问码</strong><span>在电脑的「设置 → 连接手机」查看 8 位访问码。链接不包含访问码。</span></div></li></ol>
        <div className="phone-link-actions"><label><span className="sr-only">手机访问链接</span><input ref={inputRef} readOnly value={safeUrl} onFocus={(event) => event.target.select()} /></label><button type="button" className="button primary" onClick={() => void copy()}>{copied ? <Check /> : <Copy />}{copied ? '已复制链接' : '复制链接'}</button></div>
        <div className="phone-link-footer"><a href={safeUrl} target="_blank" rel="noreferrer">{safeUrl}<ExternalLink /></a><span role="status">{copyError || (copied ? '链接已复制，可发给自己的手机。' : '这是本机提供的局域网地址；是否可达仍取决于手机网络。')}</span></div>
      </div>
    </div> : <div className="phone-access-unavailable"><Wifi /><div><strong>还没有可供手机访问的地址</strong><p>请在电脑开启带访问码的局域网访问，再刷新此页。localhost 或 127.0.0.1 只能在电脑本机打开，不能拿给手机扫码。</p><Link to="/settings#mobile-access">查看电脑连接设置</Link></div></div>}
    <details className="phone-help"><summary>扫码后打不开？</summary><p>先确认电脑仍在运行、两台设备没有连接访客 Wi-Fi；再检查电脑防火墙是否允许物忆使用专用网络。不要关闭整个防火墙。手机切到流量网络时，此局域网地址无法访问。</p></details>
  </section>
}
