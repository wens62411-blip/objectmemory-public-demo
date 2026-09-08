> 公开历史文档副本：本机身份信息已脱敏，私人数据库与实拍媒体未公开。历史结果不代表当前版本通过验收；当前限制请见 README。

# Registered phone identity: independent offline check

Scope: source-frame and registered-photo reads only. SQLite opened with `mode=ro`. No business API writes, camera state changes, production code changes, profile rebuild, threshold relaxation, or external image upload.

Evidence: `result.json`, reproducible with `.venv\Scripts\python.exe -B data/verification/phone-complete-20260906/identity-review/probe.py` from the existing project.

## Findings

- P1 / user-visible core failure: `services/vision/engine.py:1832` only invokes identity matching for existing generic detector boxes. At `services/vision/detectors/appearance.py:248`, category pre-filtering means unmatched categories have no DINO calculation. A photo-ready profile cannot locate its target in the whole frame independently.
- This is not fixed safely by simply removing the category gate. The current v7 profile scored only **0.7557769** against a manually selected complete-phone crop, below the unchanged **0.82** threshold. Four right-angle rotations and six finer rotations did not reach the threshold. The whole scene scored **0.7381721** despite being an unsuitable object bounding box; threshold relaxation would mix localization quality with identity evidence.
- The user phone is visibly present in `live-frame/actual-source.jpg`. The manual diagnostic ROI is `[307,137,328,520]` in 1280×720 coordinates. This ROI is explicitly **not an automatic detection result**, and it is never sent into the production pipeline.
- Full-frame reference-photo SIFT and ORB checks used gray/CLAHE input and Lowe ratio values 0.65/0.70/0.75/0.80. Current-frame SIFT produced at most four RANSAC inliers and invalid/non-convex projected quadrilaterals. With the production-exact crop endpoints, ORB produced at most six inliers and a non-convex projected quadrilateral (16.7% inlier ratio). No-phone controls also produced incidental four-to-six-point fits. These are insufficient geometric proofs, not successful matches.
- Existing v7 DINO loading itself is healthy. Reference-self scoring is checked in the report; all crop boundaries use the same endpoint rounding as the production registration path. No claim of a verified current-state or movement event follows from this offline check.

## Minimal safe direction

1. Keep the existing conservative category and identity checks while evaluating a separate category-independent **registered-photo localization** path. Its output must include actual object-region evidence, not a whole scene or a guessed rectangle.
2. Require reliable spatial coverage, distinct correspondence points, sane projected bounds and held-out negative examples before allowing a geometric route to create observations. Current SIFT/ORB results fail this bar.
3. The present single registered reference includes fingers/background and a substantially different view. Confirmed multi-view images and object-region quality guidance are an appropriate product improvement, but a new reference cannot be silently added from a guessed live identity. Front and back of an unseen phone cannot be inferred as the same physical identity merely from category or color.
4. Verify new localization + existing identity + multi-frame observation on held-out real frames, then movement/hand-separation evidence independently. Do not equate a manual oracle crop, a model experiment, or a public-video pass with this user's real camera closed-loop pass.

Limit: two local no-phone controls and one complete-phone source frame are a bounded diagnostic, not a general identity accuracy study. No production fix is proposed as verified by these results.
