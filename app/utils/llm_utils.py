# app/utils/llm_utils.py
import base64
import requests
import json
import re
import time # Keep for potential future use, but removing hardcoded sleep for now
from mimetypes import guess_type
from typing import Optional, List, Dict, Tuple, Any

from app.core.config import settings

def encode_image_bytes_to_base64_data_uri(image_bytes: bytes, filename: str) -> Optional[str]:
    """将图片字节流编码为 Base64 data URI 字符串"""
    try:
        mime_type, _ = guess_type(filename)
        if not mime_type or not mime_type.startswith("image"):
            extension = filename.rsplit('.', 1)[-1].lower()
            mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
            mime_type = mime_map.get(extension)
            if not mime_type:
                print(f"警告: 无法从文件名 {filename} 确定MIME类型。")
                return None
        encoded_string = base64.b64encode(image_bytes).decode('utf-8')
        return f"data:{mime_type};base64,{encoded_string}"
    except Exception as e:
        print(f"错误: 编码图片字节流 {filename} 失败: {e}")
        return None

def extract_json_from_string(text_content: Optional[str]) -> Optional[str]:
    """从可能包含Markdown标记的字符串中提取JSON部分。"""
    if not text_content or not isinstance(text_content, str):
        return None
    # 尝试匹配 ```json ... ``` 或 ``` ... ```
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text_content, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    
    # 如果没有Markdown标记，尝试直接解析从第一个 '{' 到最后一个 '}' 的内容
    first_brace = text_content.find('{')
    last_brace = text_content.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        potential_json = text_content[first_brace : last_brace + 1]
        try:
            # 验证它是否是有效的JSON
            json.loads(potential_json)
            return potential_json
        except json.JSONDecodeError:
            # 如果不是有效的JSON，可能意味着它不是一个完整的JSON对象，或者格式错误
            pass # Fall through, maybe it's plain text that happens to have braces

    # 如果以上都不匹配，且字符串以 "{" 和 "}" 包裹，则假定整个字符串是JSON
    if text_content.strip().startswith("{") and text_content.strip().endswith("}"):
        return text_content.strip()
        
    print(f"警告: 未能在响应中找到清晰的JSON结构: '{text_content[:100]}...'")
    return None # 或者返回 text_content.strip() 如果允许非JSON纯文本

def _call_llm_for_specific_tags(
    base64_image_data_url: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    expected_json_key: str,
    image_name_for_log: str
) -> Tuple[Optional[List[str]], Optional[str]]:
    """对LLM API进行单次调用以获取特定类型的标签。"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.LLM_API_KEY}"
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": base64_image_data_url}}
        ]
    })
    payload = {
        "model": settings.LLM_MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1 # Consistent with tests/llm_preprocess.py
    }
    api_url = f"{settings.LLM_API_BASE_URL.rstrip('/')}/v1/chat/completions"

    log_prefix = f"LLM API Call ({image_name_for_log} - {expected_json_key})"

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=settings.LLM_REQUEST_TIMEOUT)
        response.raise_for_status()
        api_response_json = response.json()

        if settings.LLM_DEBUG_MODE:
            print(f"--- DEBUG: {log_prefix} Raw Response ---")
            print(json.dumps(api_response_json, indent=2, ensure_ascii=False))
            print(f"--- END DEBUG ---")
        
        message_content_str = api_response_json.get("choices", [{}])[0].get("message", {}).get("content")
        
        if settings.LLM_DEBUG_MODE and message_content_str:
            print(f"--- DEBUG: {log_prefix} Message Content Before Extraction ---")
            print(message_content_str)
            print(f"--- END DEBUG ---")

        if not message_content_str:
            return None, f"{log_prefix}: API响应中未找到预期的message content。"

        json_text_to_parse = extract_json_from_string(message_content_str)

        if settings.LLM_DEBUG_MODE and json_text_to_parse:
            print(f"--- DEBUG: {log_prefix} JSON Text After Extraction ---")
            print(json_text_to_parse)
            print(f"--- END DEBUG ---")

        if not json_text_to_parse:
            return None, f"{log_prefix}: 未能从模型响应中提取任何有效内容。原始content: {message_content_str[:100]}..."
        
        try:
            parsed_data = json.loads(json_text_to_parse)
            if isinstance(parsed_data, dict) and expected_json_key in parsed_data and isinstance(parsed_data[expected_json_key], list):
                tags = [str(tag).strip() for tag in parsed_data[expected_json_key] if isinstance(tag, (str, int, float))]
                return tags, None # Success
            else:
                return None, f"{log_prefix}: 模型返回的JSON结构不符合预期。解析后JSON: {parsed_data}, 原始content: {message_content_str[:100]}..."
        except json.JSONDecodeError:
            return None, f"{log_prefix}: 模型返回的content (提取后) 不是有效的JSON。提取后文本: '{json_text_to_parse[:100]}...'"
            
    except requests.exceptions.Timeout:
        # Timeouts are often retryable
        print(f"{log_prefix}: 请求超时 ({settings.LLM_REQUEST_TIMEOUT}秒). Re-raising for potential retry.")
        raise # Re-raise requests.exceptions.Timeout
    except requests.exceptions.HTTPError as e:
        if e.response is not None and 500 <= e.response.status_code <= 599:
            # For 5xx server errors, re-raise to allow Celery task to retry
            error_detail = e.response.text[:200] if e.response.text else "No response body"
            print(f"{log_prefix}: HTTP 5xx Error encountered ({e.response.status_code} - {error_detail}). Re-raising for retry.")
            raise # Re-raise the original HTTPError
        else:
            # For 4xx client errors or other HTTP errors, log and return error message (not typically retryable by re-running the same request)
            error_text = e.response.text[:200] if e.response and e.response.text else "No response body or non-text response"
            status_code_text = e.response.status_code if e.response else "N/A"
            print(f"{log_prefix}: HTTP non-5xx Error: {status_code_text} - {error_text}...")
            return None, f"{log_prefix}: HTTP错误: {status_code_text} - {error_text}..."
    except requests.exceptions.RequestException as e: # Catch other network related errors like ConnectionError
        print(f"{log_prefix}: Network error during LLM API request: {e}. Re-raising for potential retry.")
        raise # Re-raise for Celery to handle
    except Exception as e: # Catch-all for other unexpected errors within this function
        print(f"{log_prefix}: 处理时发生未知错误: {e}")
        # Depending on policy, might want to raise this too, or return error.
        # For now, let's return error to avoid retrying unknown internal processing issues here.
        return None, f"{log_prefix}: 处理时发生未知错误: {e}"

def get_two_stage_tags_for_image_bytes(image_bytes: bytes, filename: str) -> Dict[str, Any]:
    """为图片字节流执行两阶段打标（情感和语义）。"""
    result: Dict[str, Any] = {
        "emotion_tags_list": None, # Parsed list of strings
        "semantic_tags_list": None, # Parsed list of strings
        "emotion_tags_json_str": None, # Raw JSON string for DB
        "semantic_tags_json_str": None, # Raw JSON string for DB
        "errors": []
    }

    if not settings.LLM_API_KEY or not settings.LLM_API_BASE_URL:
        result["errors"].append("LLM API Key 或 Base URL 未在配置中设置。")
        return result

    base64_image_data_url = encode_image_bytes_to_base64_data_uri(image_bytes, filename)
    if not base64_image_data_url:
        result["errors"].append("无法将图片编码为 Base64 data URI。")
        return result

    # Stage 1: Emotion Tags
    print(f"开始获取情感标签 for {filename}...")
    emotion_tags_list, emotion_error = _call_llm_for_specific_tags(
        base64_image_data_url,
        settings.LLM_SYSTEM_PROMPT_EMOTION_CN,
        settings.LLM_USER_PROMPT_EMOTION_CN,
        settings.LLM_MAX_TOKENS_EMOTION,
        "emotion_tags",
        filename
    )
    if emotion_tags_list is not None:
        result["emotion_tags_list"] = emotion_tags_list
        result["emotion_tags_json_str"] = json.dumps(emotion_tags_list, ensure_ascii=False) # Store as JSON string
        print(f"情感标签 for {filename}: {emotion_tags_list}")
    if emotion_error:
        result["errors"].append(f"情感标签获取失败: {emotion_error}")
        print(f"错误 (情感标签, {filename}): {emotion_error}")

    # Stage 2: Semantic Tags
    # Note: Removed time.sleep(5) that was in tests/llm_preprocess.py for API responsiveness
    print(f"开始获取语义标签 for {filename}...")
    semantic_tags_list, semantic_error = _call_llm_for_specific_tags(
        base64_image_data_url,
        settings.LLM_SYSTEM_PROMPT_SEMANTIC_CN,
        settings.LLM_USER_PROMPT_SEMANTIC_CN,
        settings.LLM_MAX_TOKENS_SEMANTIC,
        "semantic_tags",
        filename
    )
    if semantic_tags_list is not None:
        result["semantic_tags_list"] = semantic_tags_list
        result["semantic_tags_json_str"] = json.dumps(semantic_tags_list, ensure_ascii=False) # Store as JSON string
        print(f"语义标签 for {filename}: {semantic_tags_list}")
    if semantic_error:
        result["errors"].append(f"语义标签获取失败: {semantic_error}")
        print(f"错误 (语义标签, {filename}): {semantic_error}")
        
    return result
