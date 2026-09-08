import type { CandidateProvenance } from '../types'

// Either marker is sufficient to keep auxiliary geometry proposals display-only.
export function isShapeCandidate(candidate: CandidateProvenance) {
  return candidate.category_evidence === 'geometry_only' || candidate.proposal_backend === 'phone_shape_proposal'
}

const REJECTION_LABELS: Record<string, string> = {
  below_threshold: '外观相似度不足，尚不能确认是你的物品。',
  ambiguous_identity: '多个注册物品外观过于相似，无法可靠区分。',
  multiple_spatial_candidates: '画面中有多个相似物体，暂时不能确定哪一个是你的物品。',
  insufficient_detail: '物品在画面中太小，细节不足；请让摄像头看得更清楚。',
  profile_or_model_not_ready: '识别档案或本地模型尚未就绪。',
  no_ready_profiles: '尚无已启用的识别档案。',
  no_ready_profiles_for_category: '这个类别尚无已启用的识别档案。',
  unsupported_category: '当前识别档案不支持这个物品类别。',
  encoder_unavailable: '本地外观模型暂时不可用，不代表画面中没有物品。',
  encoder_error: '本地外观分析失败，没有据此更新位置。',
  shape_requires_category_confirmation: '只有外形轮廓建议，尚未确认类别和身份，不更新位置。',
}

export function recognitionRejection(reason?: string | null) {
  return reason ? REJECTION_LABELS[reason] || reason : '本帧尚未确认身份，不写成位置记录。'
}
