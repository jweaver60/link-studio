# Third-party notices

## MediaPipe Tasks and model assets

Link Studio uses the MediaPipe Tasks Python runtime and redistributes two Google MediaPipe model
assets under the Apache License 2.0:

- `models/selfie_segmenter.tflite`, SHA-256
  `191ac9529ae506ee0beefa6b2c945a172dab9d07d1e802a290a4e4038226658b`
- `models/face_landmarker.task`, SHA-256
  `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`

Copyright The MediaPipe Authors. MediaPipe is available from
<https://github.com/google-ai-edge/mediapipe> under Apache-2.0. The model files were obtained from
Google's public MediaPipe model storage. The Apache License is available at
<https://www.apache.org/licenses/LICENSE-2.0>.

## Optional Whisper model

`scripts/setup-local-ai` optionally downloads the multilingual `ggml-base.bin` model from the
official `ggerganov/whisper.cpp` Hugging Face repository. The helper verifies SHA-256
`60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe`. The repository identifies
the converted model collection as MIT-licensed. This model is not included in Link Studio's source
or distribution archive.

## Interoperability research

Link Studio's direct-control implementation was informed by public reverse-engineering research in
[csmarshall/link-ctl](https://github.com/csmarshall/link-ctl), Copyright (c) 2026 Charles Marshall,
licensed under the MIT License. That project documented Link 2 UVC Extension Unit selectors, the
feature mask, AI-mode buffer, and streaming-aware mode handshake.

Historical protobuf/control names were also documented by
[creatorsgarten/insta360-link-controller](https://github.com/creatorsgarten/insta360-link-controller).

Insta360 and Insta360 Link are trademarks of their respective owner. Link Studio is an independent
project and is not affiliated with or endorsed by Insta360.
