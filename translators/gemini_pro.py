from google.api_core.exceptions import InternalServerError
from translators.base import BaseTranslator

import google.generativeai as genai
import asyncio
import codecs


class GeminiProTranslator(BaseTranslator):
    # based on https://ai.google.dev/available_regions#available_languages
    # make sure that you have access to Gemini Region
    language_mapping = {
        "en": "English",
        "pt": "Portuguese",
        "pt-BR": "Portuguese",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "nl": "Dutch",
        "it": "Italian",
        "ko": "Korean",
        "zh": "Chinese",
        "uk": "Ukrainian",
        "uk-UA": "Ukrainian",
        "ja": "Japan",
        "pl": "Polish",
        "ar": "Arabic",
        "bn": "Bengali",
        "bg": "Bulgarian",
        "hr": "Croatian",
        "cs": "Czech",
        "da": "Danish",
        "et": "Estonian",
        "fi": "Finnish",
        "el": "Greek",
        "iw": "Hebrew",
        "hi": "Hindi",
        "hu": "Hungarian",
        "id": "Indonesian",
        "lv": "Latvian",
        "lt": "Lithuanian",
        "no": "Norwegian",
        "ro": "Romanian",
        "ru": "Russian",
        "sr": "Serbian",
        "sk": "Slovak",
        "sl": "Slovenian",
        "sw": "Swahili",
        "sv": "Swedish",
        "th": "Thai",
        "tr": "Turkish",
        "vi": "Vietnamese"
    }

    def __init__(self, access_token, max_length):
        if access_token is None:
            raise Exception("Access token is required!")
        super().__init__(None, None, None, None, max_length)
        genai.configure(api_key=access_token)
        self.printed_error_langs = {}
        self.model = genai.GenerativeModel('gemini-pro')

    async def translate_text(self, text, prompt):
        try:
            ## Need to ignore safety to correctly translate input from different languages
            result = self.model.generate_content_async(f"{prompt}\n{text}", safety_settings={'HARASSMENT': 'block_none',
                                                                                'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'block_none',
                                                                                'harm_category_dangerous_content': 'block_none',
                                                                                'harm_category_hate_speech': 'block_none',
                                                                                'harm_category_harassment': 'block_none'
                                                                                })
            return await result
        except InternalServerError:
            return await self.translate_text(text, prompt)
    def decode_result(self, response):
        try:
            return response.text
        except:
            try:
                result = "".join(map(lambda part: part.text, response.parts))
                decoded_result = codecs.escape_decode(result)[0].decode("utf8")
                return decoded_result
            except:
                if len(response.candidates) == 0:
                    return
                result = "".join(map(lambda part: part.text, response.candidates[0].content.parts))
                decoded_result = codecs.escape_decode(result)[0].decode("utf8")
                return decoded_result

    async def translate_texts(self, texts, prompt):
        tasks = []
        for text in texts:
            tasks.append(self.translate_text(text, prompt))
            await asyncio.sleep(1)
        results = await asyncio.gather(*tasks)
        decoded_results = []
        for i in range(0,len(results)):
            try:
                decoded_results.append(self.decode_result(results[i]))
            except:
                print("Error during translation, returning source language")
                decoded_results.append(texts[i])

        return decoded_results

    def translate(self, texts, source_lang, target_lang):
        if len(texts) > 60:
            raise Exception("Batch size cannot be more than 60 for this translator due ratelimit in 60 RPM!")
        if source_lang in self.language_mapping and target_lang in self.language_mapping:
            trgt_lang = self.language_mapping[target_lang]
            prompt = (f"Translate text below to {trgt_lang} language and preserve formatting and special characters. "
                      f"Respond with translated text ONLY. Here is text to translate:\n")
            loop = asyncio.get_event_loop()
            result = loop.run_until_complete(self.translate_texts(texts, prompt))
            return result
        else:
            if not (source_lang in self.printed_error_langs):
                print(
                    f"[---- LLaMa2Lang ----] Gemini Pro cannot translate from source language {source_lang} or to your target language {target_lang}, returning originals")
                self.printed_error_langs[source_lang] = True
            return None
