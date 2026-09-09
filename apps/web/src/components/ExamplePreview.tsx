import { MapPin, SearchX } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Modal, Segmented } from './UI'

export default function ExamplePreview({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<'seen' | 'unknown'>('seen')
  return <Modal title="先看看物忆怎样回答" description="下面是交互示例，不是你的摄像头或物品记录。" onClose={onClose}>
    <div className="wizard-body example-preview">
      <Badge tone="warning">示例说明 · 非真实记录</Badge>
      <p>试着问：“我的钥匙在哪里？”</p>
      <Segmented label="选择示例情形" value={state} onChange={setState} options={[{ value: 'seen', label: '有观察记录' }, { value: 'unknown', label: '没有记录' }]} />
      <div className="example-answer" aria-live="polite">
        {state === 'seen' ? <><MapPin /><h3>示例：最后在玄关桌面看到钥匙</h3><p>示例时间：今天 10:24。这是最后看到的位置，不代表现在仍在那里。</p><p>接入后，有图像证据时会显示对应截图，帮助你判断。</p></> : <><SearchX /><h3>示例：还没有钥匙的位置记录</h3><p>先添加钥匙照片，再把它放到已连接的摄像头前。没有可靠记录时，物忆不会猜一个位置。</p></>}
      </div>
      <div className="empty-actions"><Link className="button primary" to="/cameras?add=1" onClick={onClose}>连接设备开始使用</Link><button className="button secondary" onClick={onClose}>继续浏览</button></div>
      <p className="muted">示例仅在当前窗口展示，不保存到物品、事件或设备列表。</p>
    </div>
  </Modal>
}
