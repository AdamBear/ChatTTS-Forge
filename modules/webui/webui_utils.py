from typing import Union

import gradio as gr
import numpy as np
import torch
import torch.profiler

from modules import refiner
from modules.api.utils import calc_spk_style
from modules.data import styles_mgr
from modules.Enhancer.ResembleEnhance import apply_audio_enhance as _apply_audio_enhance
from modules.normalization import text_normalize
from modules.SentenceSplitter import SentenceSplitter
from modules.speaker import Speaker, speaker_mgr
from modules.ssml_parser.SSMLParser import SSMLBreak, SSMLSegment, create_ssml_parser
from modules.synthesize_audio import synthesize_audio
from modules.SynthesizeSegments import SynthesizeSegments, combine_audio_segments
from modules.utils import audio
from modules.utils.hf import spaces
from modules.webui import webui_config


def get_speakers():
    return speaker_mgr.list_speakers()


def get_speaker_names() -> tuple[list[Speaker], list[str]]:
    speakers = get_speakers()

    def get_speaker_show_name(spk):
        if spk.gender == "*" or spk.gender == "":
            return spk.name
        return f"{spk.gender} : {spk.name}"

    speaker_names = [get_speaker_show_name(speaker) for speaker in speakers]
    speaker_names.sort(key=lambda x: x.startswith("*") and "-1" or x)

    return speakers, speaker_names


def get_styles():
    return styles_mgr.list_items()


def load_spk_info(file):
    if file is None:
        return "empty"
    try:

        spk: Speaker = Speaker.from_file(file)
        infos = spk.to_json()
        return f"""
- name: {infos.name}
- gender: {infos.gender}
- describe: {infos.describe}
    """.strip()
    except:
        return "load failed"


def segments_length_limit(
    segments: list[Union[SSMLBreak, SSMLSegment]], total_max: int
) -> list[Union[SSMLBreak, SSMLSegment]]:
    ret_segments = []
    total_len = 0
    for seg in segments:
        if isinstance(seg, SSMLBreak):
            ret_segments.append(seg)
            continue
        total_len += len(seg["text"])
        if total_len > total_max:
            break
        ret_segments.append(seg)
    return ret_segments


@torch.inference_mode()
@spaces.GPU(duration=120)
def apply_audio_enhance(audio_data, sr, enable_denoise, enable_enhance):
    return _apply_audio_enhance(audio_data, sr, enable_denoise, enable_enhance)


@torch.inference_mode()
@spaces.GPU(duration=120)
def synthesize_ssml(
    ssml: str,
    batch_size=4,
    enable_enhance=False,
    enable_denoise=False,
    eos: str = "[uv_break]",
    spliter_thr: int = 100,
    progress=gr.Progress(track_tqdm=True),
):
    try:
        batch_size = int(batch_size)
    except Exception:
        batch_size = 8

    ssml = ssml.strip()

    if ssml == "":
        return None

    parser = create_ssml_parser()
    segments = parser.parse(ssml)
    max_len = webui_config.ssml_max
    segments = segments_length_limit(segments, max_len)

    if len(segments) == 0:
        return None

    synthesize = SynthesizeSegments(
        batch_size=batch_size, eos=eos, spliter_thr=spliter_thr
    )
    audio_segments = synthesize.synthesize_segments(segments)
    combined_audio = combine_audio_segments(audio_segments)

    sr = combined_audio.frame_rate
    audio_data, sr = apply_audio_enhance(
        audio.audiosegment_to_librosawav(combined_audio),
        sr,
        enable_denoise,
        enable_enhance,
    )

    # NOTE: 这里必须要加，不然 gradio 没法解析成 mp3 格式
    audio_data = audio.audio_to_int16(audio_data)

    return sr, audio_data


# @torch.inference_mode()
@spaces.GPU(duration=120)
def tts_generate(
    text,
    temperature=0.3,
    top_p=0.7,
    top_k=20,
    spk=-1,
    infer_seed=-1,
    use_decoder=True,
    prompt1="",
    prompt2="",
    prefix="",
    style="",
    disable_normalize=False,
    batch_size=4,
    enable_enhance=False,
    enable_denoise=False,
    spk_file=None,
    spliter_thr: int = 100,
    eos: str = "[uv_break]",
    progress=gr.Progress(track_tqdm=True),
):
    try:
        batch_size = int(batch_size)
    except Exception:
        batch_size = 4

    max_len = webui_config.tts_max
    text = text.strip()[0:max_len]

    if text == "":
        return None

    if style == "*auto":
        style = None

    if isinstance(top_k, float):
        top_k = int(top_k)

    params = calc_spk_style(spk=spk, style=style)
    spk = params.get("spk", spk)

    infer_seed = infer_seed or params.get("seed", infer_seed)
    temperature = temperature or params.get("temperature", temperature)
    prefix = prefix or params.get("prefix", prefix)
    prompt1 = prompt1 or params.get("prompt1", "")
    prompt2 = prompt2 or params.get("prompt2", "")

    infer_seed = np.clip(infer_seed, -1, 2**32 - 1, out=None, dtype=np.float64)
    infer_seed = int(infer_seed)

    if not disable_normalize:
        text = text_normalize(text)

    if spk_file:
        spk = Speaker.from_file(spk_file)

    sample_rate, audio_data = synthesize_audio(
        text=text,
        temperature=temperature,
        top_P=top_p,
        top_K=top_k,
        spk=spk,
        infer_seed=infer_seed,
        use_decoder=use_decoder,
        prompt1=prompt1,
        prompt2=prompt2,
        prefix=prefix,
        batch_size=batch_size,
        end_of_sentence=eos,
        spliter_threshold=spliter_thr,
    )

    audio_data, sample_rate = apply_audio_enhance(
        audio_data, sample_rate, enable_denoise, enable_enhance
    )
    # NOTE: 这里必须要加，不然 gradio 没法解析成 mp3 格式
    audio_data = audio.audio_to_int16(audio_data)
    return sample_rate, audio_data


@torch.inference_mode()
@spaces.GPU(duration=120)
def refine_text(
    text: str,
    prompt: str,
    progress=gr.Progress(track_tqdm=True),
):
    text = text_normalize(text)
    return refiner.refine_text(text, prompt=prompt)


@torch.inference_mode()
@spaces.GPU(duration=120)
def split_long_text(long_text_input):
    spliter = SentenceSplitter(webui_config.spliter_threshold)
    sentences = spliter.parse(long_text_input)
    sentences = [text_normalize(s) for s in sentences]
    data = []
    for i, text in enumerate(sentences):
        data.append([i, text, len(text)])
    return data
