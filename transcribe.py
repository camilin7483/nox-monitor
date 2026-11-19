#!/usr/bin/env python3
import sys, json, os, warnings
from faster_whisper import WhisperModel

MODEL_SIZE = os.environ.get('WHISPER_MODEL', 'base')
DEVICE = os.environ.get('WHISPER_DEVICE', 'cpu')
COMPUTE_TYPE = os.environ.get('WHISPER_COMPUTE_TYPE', 'int8')

def transcribe(audio_path):
    try:
        model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        segments, info = model.transcribe(audio_path, beam_size=5, language='es')
        text = ' '.join(seg.text for seg in segments)
        return {'text': text.strip(), 'language': info.language, 'duration': info.duration}
    except Exception as e:
        return {'error': str(e)}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: transcribe.py <audio_path>'}))
        sys.exit(1)
    audio_path = sys.argv[1]
    if not os.path.isfile(audio_path):
        print(json.dumps({'error': f'File not found: {audio_path}'}))
        sys.exit(1)
    result = transcribe(audio_path)
    print(json.dumps(result))
