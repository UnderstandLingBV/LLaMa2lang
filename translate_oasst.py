import os
import torch
from datasets import load_dataset
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    T5ForConditionalGeneration,
    T5Tokenizer,
    BitsAndBytesConfig
)
import json
import re
import gc
from tqdm import tqdm
import argparse

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# Cache for loaded translation models, seemingly faster than letting Huggingface handle it
model_cache = {}

# Alternative models that are not created by Helsink-NLP
alternative_models = {
    "en-pl": 'gsarti/opus-mt-tc-en-pl',
    "en-ja": 'gsarti/opus-mt-tc-base-en-ja'
}

def load_model(model_name, model_key):
  tokenizer = AutoTokenizer.from_pretrained(model_name)
  model = AutoModelForSeq2SeqLM.from_pretrained(model_name).to(device)
  model_cache[model_key] = (model, tokenizer)
  return model, tokenizer

# Tries to obtain a translation model from the Helsinki-NLP groups OPUS models. Returns None, None if no model is found for this language pair
def get_helsinki_nlp_model(source_lang, target_lang):
    # Small fix for odd language codes
    if source_lang == 'pt-BR':
        source_lang = 'bzs'
    if source_lang == 'uk-UA':
        source_lang = 'uk'
    model_key = f'{source_lang}-{target_lang}'

    if model_key in model_cache:
        return model_cache[model_key]

    model_name = f'Helsinki-NLP/opus-mt-{source_lang}-{target_lang}'
    try:
      return load_model(model_name, model_key)
    except Exception as e:
      # Try to load the tc-big naming convention files
      try:
        model_name = f'Helsinki-NLP/opus-mt-tc-big-{source_lang}-{target_lang}'
        return load_model(model_name, model_key)
      except Exception as e:
        try:
          model_name = alternative_models[model_key]
          return load_model(model_name, model_key)
        except Exception as e:
          return None, None

def batch_translate(texts, source_lang, target_lang, intermediate_lang = 'en'):
    model, tokenizer = get_helsinki_nlp_model(source_lang, target_lang)
    if model is None or tokenizer is None:
      # Try via intermediate language
      model_i, tokenizer_i = get_helsinki_nlp_model(source_lang, intermediate_lang)
      model_t, tokenizer_t = get_helsinki_nlp_model(intermediate_lang, target_lang)
      if model_i is None or tokenizer_i is None or model_t is None or tokenizer_t is None:
        return None

      # To intermediate language first
      inputs = tokenizer_i(texts, padding=True, truncation=True, max_length=1024, return_tensors="pt").to(device)
      with torch.no_grad():
          translated_outputs = model_i.generate(inputs.input_ids, max_length=1024)
      intermediate_texts = [tokenizer_i.decode(output, skip_special_tokens=True) for output in translated_outputs]

      # Now to target
      inputs = tokenizer_t(intermediate_texts, padding=True, truncation=True, max_length=1024, return_tensors="pt").to(device)
      with torch.no_grad():
          translated_outputs = model_t.generate(inputs.input_ids, max_length=1024)
      translated_texts = [tokenizer_t.decode(output, skip_special_tokens=True) for output in translated_outputs]
      return translated_texts
    else:
      inputs = tokenizer(texts, padding=True, truncation=True, max_length=1024, return_tensors="pt").to(device)
      with torch.no_grad():
          translated_outputs = model.generate(inputs.input_ids, max_length=1024)
      translated_texts = [tokenizer.decode(output, skip_special_tokens=True) for output in translated_outputs]
      return translated_texts

def translate_madlad(texts, target_lang):
    # Get madlad
    model, tokenizer = model_cache['madlad']
    translated_texts = []
    for text in texts:
        # Add the target language to the text
        madlad_text = f'<2{target_lang}> ' + text.replace("\n", " ")
        input_ids = tokenizer(madlad_text, return_tensors="pt").input_ids.to(device)
        outputs = model.generate(input_ids=input_ids, max_new_tokens=1024)
        # Decoding outputs
        translated_texts.append(tokenizer.decode(outputs[0], skip_special_tokens=True))

    return translated_texts
    

# Find the max checkpoint number to continue from
def find_largest_checkpoint(checkpoint_location):
    pattern = r'upto_(\d+).json'
    files = os.listdir(checkpoint_location)
    numbers = [int(re.search(pattern, file).group(1)) for file in files if re.match(pattern, file)]
    if numbers:
        return max(numbers)
    else:
        return 0

# Group all records in a dataset by language so we can use a single model in a batched fashion
def group_records_by_language(dataset, lang_field):
    grouped_records = {}
    for record in dataset:
        lang = record[lang_field]
        if lang not in grouped_records:
            grouped_records[lang] = []
        grouped_records[lang].append(record)
    return grouped_records

def main():
    parser = argparse.ArgumentParser(description="Translate a dataset (default: oasst1) to target language")
    parser.add_argument('target_lang', type=str, 
                        help="The target language, using language codes as used in Helsinki-NLP's OPUS translation models OR the codes used by madlad (see original paper in the Appendix)")
    parser.add_argument('checkpoint_location', type=str, 
                        help="The folder the script will write (JSONized) checkpoint files to. Folder will be created if it doesn't exist.")
    parser.add_argument('--base_dataset', type=str, default="OpenAssistant/oasst1",
                        help="The base dataset to translate, defaults to OpenAssistant/oasst1")
    parser.add_argument('--base_dataset_text_field', type=str, default="text",
                        help="The base dataset's column name containing the actual text to translate. Defaults to text")
    parser.add_argument('--base_dataset_lang_field', type=str, default="lang",
                        help="The base dataset's column name containing the language the source text was written in. Defaults to lang")
    parser.add_argument('--checkpoint_n', type=int, default=400,
                        help="An integer representing how often a checkpoint file will be written out. For OASST1, 400 is a reasonable number.")
    parser.add_argument('--batch_size', type=int, default=20,
                        help="The batch size for a single translation model. Adjust based on your GPU capacity. Default is 20.")
    parser.add_argument('--use_madlad', action='store_true',
                        help='Optional flag to use the MADLAD model google/madlad400-3b-mt. If set, the script will use MADLAD. Default is False.')
    parser.add_argument('--madlad_quant', action='store_true',
                        help='Optional flag that can be set when using MADLAD through use_madlad. If set, the MADLAD model is quantized to 8 bits when loaded, adviced for <= 16GB vRAM, also use batch_size <= 40 in that case')
    parser.add_argument('--madlad_quant4', action='store_true',
                        help='Optional flag that can be set when using MADLAD through use_madlad. If set, the MADLAD model is quantized to 4 bits when loaded.')

    args = parser.parse_args()
    target_lang = args.target_lang
    checkpoint_location = args.checkpoint_location
    checkpoint_n = args.checkpoint_n
    batch_size = args.batch_size
    base_dataset = args.base_dataset
    base_dataset_text_field = args.base_dataset_text_field
    base_dataset_lang_field = args.base_dataset_lang_field
    use_madlad = args.use_madlad
    madlad_quant = args.madlad_quant
    madlad_quant4 = args.madlad_quant4

    if checkpoint_n % batch_size != 0:
        raise Exception("Checkpoint N must be a multiple of batch size!")

    # Load the open assistant/base dataset
    dataset = load_dataset(base_dataset)

    # Set up madlad if required
    if use_madlad:
        if madlad_quant4:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16
            )
            model = T5ForConditionalGeneration.from_pretrained("google/madlad400-3b-mt", device_map=device, quantization_config=bnb_config, load_in_4bit=True)
        else:
            if madlad_quant:
                model = T5ForConditionalGeneration.from_pretrained("google/madlad400-3b-mt", device_map=device, load_in_8bit=True)
            else:
                model = T5ForConditionalGeneration.from_pretrained("google/madlad400-3b-mt", device_map=device)
        tokenizer = T5Tokenizer.from_pretrained("google/madlad400-3b-mt")
        model_cache['madlad'] = (model, tokenizer)

    # Loop through the actual data and translate
    with tqdm(total=sum(len(split) for split in dataset.values())) as pbar:
        for fold in dataset:
            records_by_lang = group_records_by_language(dataset[fold], base_dataset_lang_field)
            
            for source_lang, records in records_by_lang.items():
                lang_checkpoint_location = os.path.join(checkpoint_location, fold, f'from_{source_lang}')
                os.makedirs(lang_checkpoint_location, exist_ok=True)
                last_checkpoint_n = find_largest_checkpoint(lang_checkpoint_location)
                translated_texts = []
                print(f'Got {len(records)} records for source language {source_lang}, skipping {last_checkpoint_n}')
                pbar.update(last_checkpoint_n)
                for cnt in range(last_checkpoint_n, len(records), batch_size):
                    # Translate a full batch
                    batch = records[cnt:cnt+batch_size]
                    texts_to_translate = [record[base_dataset_text_field] for record in batch]
                    if not(use_madlad):
                        translated_batch = batch_translate(texts_to_translate, source_lang, target_lang)
                    else:
                        translated_batch = translate_madlad(texts_to_translate, target_lang)

                    if translated_batch is not None:
                        # Combine original record with translated text
                        for record, translation in zip(batch, translated_batch):
                            record[base_dataset_text_field] = translation
                            record[base_dataset_lang_field] = target_lang
                            translated_texts.append(record)
                    
                    pbar.update(batch_size)

                    # Write out checkpoint file
                    if (cnt + batch_size) % checkpoint_n == 0 and cnt != 0:
                        print(f"Writing out checkpoint #{str(cnt + batch_size)} for source language {source_lang}")
                        with open(os.path.join(lang_checkpoint_location, f'upto_{str(cnt + batch_size)}.json'), 'w', encoding='utf-8') as f:
                            json.dump(translated_texts, f)
                        translated_texts = []

                # Write checkpoint
                checkpoint_file = os.path.join(lang_checkpoint_location, f'upto_{cnt}.json')
                with open(checkpoint_file, 'w', encoding='utf-8') as f:
                    json.dump(batch, f)

            # One source language down, release the memory
            if device == 'cuda' and not(use_madlad):
                gc.collect()
                torch.cuda.empty_cache()

if __name__ == "__main__":
    main()