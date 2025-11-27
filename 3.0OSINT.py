import asyncio
import json
import os
import re
import time
import hashlib
import logging
from datetime import datetime
from collections import Counter
from typing import List, Dict
from telethon import TelegramClient, functions, types
from telethon.tl.types import User, Chat, Channel
import requests
from bs4 import BeautifulSoup
from config import API_CONFIG

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_osint.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TelegramOSINT:
    def __init__(self, api_id, api_hash, session_name='telegram_osint'):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.client = TelegramClient(session_name, api_id, api_hash)
        self.results = {}

    async def start_client(self):
        """Iniciar el cliente de Telegram"""
        await self.client.start()
        logger.info("Cliente de Telegram iniciado")

    def validate_telegram_input(self, input_str):
        """Validar y formatear input para Telegram"""
        input_str = input_str.strip()
        if input_str.isdigit():
            return {'type': 'phone', 'value': input_str}
        if input_str.startswith('@'):
            return {'type': 'username', 'value': input_str}
        if ' ' in input_str:
            possible_username = f"@{input_str.replace(' ', '').lower()}"
            return {'type': 'name', 'value': input_str, 'suggested_username': possible_username}
        return {'type': 'username', 'value': f"@{input_str}"}

    async def get_user_info(self, username_or_phone):
        """Obtener información completa de un usuario - VERSIÓN MEJORADA"""
        try:
            input_info = self.validate_telegram_input(username_or_phone)
            cleaned_input = input_info['value']
            logger.info(f"🔍 Buscando: {username_or_phone} -> {cleaned_input} ({input_info['type']})")

            if input_info['type'] == 'name':
                print(f"🔍 Buscando usuario por nombre: {username_or_phone}")
                if 'suggested_username' in input_info:
                    try:
                        logger.info(f"🔍 Intentando con username sugerido: {input_info['suggested_username']}")
                        entity = await self.client.get_entity(input_info['suggested_username'])
                        logger.info(f"✅ Usuario encontrado con username sugerido")
                    except Exception as e:
                        logger.info(f"❌ No se encontró con username sugerido: {e}")
                        users_found = await self.search_user_by_name(username_or_phone)
                        if users_found:
                            if len(users_found) == 1:
                                user = users_found[0]
                                if user['username'] != 'N/A':
                                    logger.info(f"🔍 Usando usuario encontrado en grupos: @{user['username']}")
                                    return await self.get_user_info(user['username'])
                            raise ValueError(f"Se encontraron múltiples usuarios. Use username específico.")
                        else:
                            raise ValueError(f"No se pudo encontrar el usuario: {username_or_phone}")
                else:
                    entity = await self.client.get_entity(cleaned_input)
            else:
                entity = await self.client.get_entity(cleaned_input)

            user_info = {
                'id': entity.id,
                'username': getattr(entity, 'username', 'N/A'),
                'first_name': getattr(entity, 'first_name', 'N/A'),
                'last_name': getattr(entity, 'last_name', 'N/A'),
                'phone': getattr(entity, 'phone', 'N/A'),
                'verified': getattr(entity, 'verified', False),
                'premium': getattr(entity, 'premium', False),
                'bot': getattr(entity, 'bot', False),
                'restricted': getattr(entity, 'restricted', False),
                'scam': getattr(entity, 'scam', False),
                'fake': getattr(entity, 'fake', False),
                'status': str(getattr(entity, 'status', 'N/A')),
                'dc_id': getattr(entity, 'dc_id', 'N/A'),
                'lang_code': getattr(entity, 'lang_code', 'N/A'),
                'photo': None,
                'photo_id': None,
                'common_chats_count': 0,
                'last_seen': None,
                'bio': getattr(entity, 'about', 'N/A')
            }

            if entity.photo:
                user_info['photo_id'] = entity.photo.photo_id
                photo_path = await self.download_profile_photo(entity)
                user_info['photo'] = photo_path

            if hasattr(entity, 'status') and entity.status:
                if hasattr(entity.status, 'was_online'):
                    user_info['last_seen'] = entity.status.was_online.isoformat()

            try:
                common_chats = await self.client(functions.messages.GetCommonChatsRequest(
                    user_id=entity.id,
                    max_id=0,
                    limit=100
                ))
                user_info['common_chats_count'] = len(common_chats.chats)
            except:
                user_info['common_chats_count'] = 0

            return user_info

        except Exception as e:
            logger.error(f"Error obteniendo información del usuario '{username_or_phone}': {e}")
            return None

    async def search_user_by_name(self, name):
        """Buscar usuario por nombre en chats y grupos"""
        try:
            logger.info(f"🔍 Buscando usuario por nombre: {name}")
            found_users = []
            async for dialog in self.client.iter_dialogs(limit=50):
                if dialog.is_group or dialog.is_channel:
                    try:
                        participants = await self.client.get_participants(dialog.entity, limit=100)
                        for participant in participants:
                            full_name = f"{getattr(participant, 'first_name', '')} {getattr(participant, 'last_name', '')}".strip()
                            if name.lower() in full_name.lower():
                                user_info = {
                                    'id': participant.id,
                                    'username': getattr(participant, 'username', 'N/A'),
                                    'first_name': getattr(participant, 'first_name', 'N/A'),
                                    'last_name': getattr(participant, 'last_name', 'N/A'),
                                    'found_in': dialog.name,
                                    'chat_type': 'group' if dialog.is_group else 'channel'
                                }
                                found_users.append(user_info)
                                logger.info(f"✅ Encontrado: {full_name} en {dialog.name}")
                    except Exception:
                        continue
            return found_users
        except Exception as e:
            logger.error(f"Error buscando por nombre: {e}")
            return []

    async def download_profile_photo(self, entity, download_folder='photos'):
        """Descargar foto de perfil"""
        try:
            if not os.path.exists(download_folder):
                os.makedirs(download_folder)
            photo_path = await self.client.download_profile_photo(
                entity,
                file=os.path.join(download_folder, f"{entity.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg")
            )
            return photo_path
        except Exception as e:
            logger.error(f"Error descargando foto: {e}")
            return None

    # --- Métodos de fotos ---
    async def get_profile_photos(self, username):
        """Obtener fotos de perfil de un usuario"""
        try:
            entity = await self.client.get_entity(username)
            photos = await self.client.get_profile_photos(entity, limit=10)
            result = []
            for photo in photos:
                photo_file = await self.client.download_media(photo, file=bytes)
                if photo_file:
                    result.append({
                        'date': photo.date,
                        'size': photo.sizes[-1] if photo.sizes else None,
                        'data': photo_file
                    })
            return result
        except Exception as e:
            print(f"❌ Error obteniendo fotos de perfil: {e}")
            return []

    async def search_public_photos(self, username, limit=100):
        """Buscar fotos en mensajes públicos"""
        try:
            entity = await self.client.get_entity(username)
            photos = []
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.photo:
                    photos.append({
                        'date': message.date,
                        'id': message.id,
                        'text': message.text,
                        'media_type': 'photo'
                    })
            return photos
        except Exception as e:
            print(f"❌ Error buscando fotos públicas: {e}")
            return []

    async def get_all_old_photos(self, username, limit=2000):
        """Extrae TODAS las fotos antiguas, incluidas las que el usuario cree borradas."""
        try:
            entity = await self.client.get_entity(username)
            photos = []
            recovery_dir = f"recovered_photos_{username}"
            os.makedirs(recovery_dir, exist_ok=True)
            logger.info(f"🔍 Buscando fotos antiguas de {username} (límite: {limit})...")
            photo_count = 0
            async for msg in self.client.iter_messages(entity, limit=limit):
                if msg.photo:
                    photo_info = {
                        "message_id": msg.id,
                        "date": msg.date.isoformat(),
                        "photo_id": msg.photo.id,
                        "saved_at": None
                    }
                    filename = f"{username}_{msg.id}_{msg.date.strftime('%Y%m%d_%H%M%S')}.jpg"
                    path = os.path.join(recovery_dir, filename)
                    try:
                        await msg.download_media(path)
                        photo_info["saved_at"] = path
                        photo_count += 1
                        logger.info(f"✅ Foto recuperada: {filename}")
                    except Exception as download_error:
                        logger.error(f"Error descargando foto {msg.id}: {download_error}")
                        photo_info["download_error"] = str(download_error)
                    photos.append(photo_info)
            logger.info(f"📸 Total de fotos recuperadas: {photo_count}")
            return photos
        except Exception as e:
            logger.error(f"Error recuperando fotos antiguas: {e}")
            return []

    # --- Análisis de mensajes ---
    def serialize_reactions(self, reactions):
        """Convierte un objeto MessageReactions a un diccionario JSON-serializable."""
        if reactions is None:
            return None
        try:
            result = {}
            for reaction_count in reactions.results:
                emoticon = getattr(reaction_count.reaction, 'emoticon', str(reaction_count.reaction))
                result[emoticon] = reaction_count.count
            return result
        except Exception as e:
            logger.debug(f"Error serializando reacciones: {e}")
            return str(reactions)

    async def get_full_message_history(self, username, limit=500):
        """Obtener el historial completo de mensajes con contenido"""
        try:
            entity = await self.client.get_entity(username)
            messages = []
            logger.info(f"📨 Obteniendo {limit} mensajes de {username}...")
            async for message in self.client.iter_messages(entity, limit=limit):
                msg_data = {
                    'id': message.id,
                    'date': message.date.isoformat(),
                    'text': message.text if message.text else '',
                    'media_type': type(message.media).__name__ if message.media else 'text',
                    'is_reply': bool(message.reply_to),
                    'is_forward': bool(message.fwd_from),
                    'views': getattr(message, 'views', 0),
                    'forwards': getattr(message, 'forwards', 0),
                    'reactions': self.serialize_reactions(getattr(message, 'reactions', None))
                }
                if message.text:
                    urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', message.text)
                    msg_data['urls'] = urls
                if message.media:
                    if hasattr(message.media, 'document'):
                        if hasattr(message.media.document, 'mime_type'):
                            msg_data['mime_type'] = message.media.document.mime_type
                        msg_data['file_size'] = getattr(message.media.document, 'size', 0)
                messages.append(msg_data)
            logger.info(f"✅ Obtenidos {len(messages)} mensajes")
            return messages
        except Exception as e:
            logger.error(f"Error obteniendo historial de mensajes: {e}")
            return []

    async def get_all_words_used(self, username, limit=1000):
        """Obtener todas las palabras únicas usadas por el usuario"""
        try:
            entity = await self.client.get_entity(username)
            all_words = Counter()
            logger.info(f"🔤 Analizando palabras de {limit} mensajes...")
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    text_clean = re.sub(r'http[s]?://\S+', '', message.text)
                    words = re.findall(r'\b[a-zA-ZáéíóúñÁÉÍÓÚÑ]+\b', text_clean.lower())
                    common_words = {
                        'el', 'la', 'de', 'que', 'y', 'en', 'un', 'es', 'se', 'no',
                        'te', 'lo', 'le', 'me', 'mi', 'tu', 'su', 'los', 'las', 'del',
                        'the', 'and', 'you', 'for', 'are', 'with', 'this', 'that', 'have'
                    }
                    filtered_words = [w for w in words if w not in common_words and len(w) > 2]
                    all_words.update(filtered_words)
            word_stats = {
                'total_unique_words': len(all_words),
                'most_common_words': all_words.most_common(50),
                'word_frequency': dict(all_words.most_common(100))
            }
            logger.info(f"✅ Encontradas {word_stats['total_unique_words']} palabras únicas")
            return word_stats
        except Exception as e:
            logger.error(f"Error analizando palabras: {e}")
            return None

    async def get_message_categories(self, username, limit=500):
        """Categorizar mensajes por tipo de contenido"""
        try:
            entity = await self.client.get_entity(username)
            categories = {
                'text_only': [],
                'with_links': [],
                'with_media': [],
                'questions': [],
                'exclamations': [],
                'long_messages': [],
                'short_messages': []
            }
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    msg_data = {
                        'id': message.id,
                        'date': message.date.isoformat(),
                        'text': message.text,
                        'length': len(message.text)
                    }
                    categories['text_only'].append(msg_data)
                    if re.search(r'http[s]?://', message.text):
                        categories['with_links'].append(msg_data)
                    if '?' in message.text:
                        categories['questions'].append(msg_data)
                    if '!' in message.text:
                        categories['exclamations'].append(msg_data)
                    if len(message.text) > 200:
                        categories['long_messages'].append(msg_data)
                    if len(message.text) < 50:
                        categories['short_messages'].append(msg_data)
                if message.media:
                    media_data = {
                        'id': message.id,
                        'date': message.date.isoformat(),
                        'media_type': type(message.media).__name__,
                        'caption': message.text if message.text else ''
                    }
                    categories['with_media'].append(media_data)

            category_stats = {
                'text_only_count': len(categories['text_only']),
                'with_links_count': len(categories['with_links']),
                'with_media_count': len(categories['with_media']),
                'questions_count': len(categories['questions']),
                'exclamations_count': len(categories['exclamations']),
                'long_messages_count': len(categories['long_messages']),
                'short_messages_count': len(categories['short_messages'])
            }
            return {'categories': categories, 'stats': category_stats}
        except Exception as e:
            logger.error(f"Error categorizando mensajes: {e}")
            return None

    async def get_conversation_topics(self, username, limit=500):
        """Identificar temas de conversación basados en palabras clave"""
        try:
            entity = await self.client.get_entity(username)
            topics_keywords = {
                'tecnología': {'tecnología', 'tecnologia', 'tech', 'software', 'hardware', 'app', 'aplicación', 'internet', 'web', 'digital', 'computadora', 'ordenador', 'móvil', 'celular', 'smartphone'},
                'programación': {'programación', 'programacion', 'código', 'codigo', 'python', 'javascript', 'java', 'html', 'css', 'desarrollo', 'developer', 'coding', 'script', 'api'},
                'videojuegos': {'juego', 'videojuego', 'gaming', 'gamer', 'play', 'jugando', 'consola', 'steam', 'nintendo', 'playstation', 'xbox', 'minecraft', 'fortnite'},
                'música': {'música', 'musica', 'canción', 'cancion', 'artista', 'banda', 'album', 'spotify', 'youtube music', 'escuchar', 'ritmo', 'melodía'},
                'películas': {'película', 'pelicula', 'cine', 'netflix', 'disney', 'amazon prime', 'serie', 'actor', 'actriz', 'director', 'guion'},
                'deportes': {'deporte', 'fútbol', 'futbol', 'baloncesto', 'tenis', 'natación', 'ejercicio', 'gimnasio', 'entrenamiento', 'partido', 'competencia'},
                'comida': {'comida', 'receta', 'cocina', 'restaurante', 'cena', 'almuerzo', 'desayuno', 'postre', 'bebida', 'receta', 'cocinar'},
                'viajes': {'viaje', 'viajar', 'vacaciones', 'turismo', 'hotel', 'avión', 'aeropuerto', 'destino', 'playa', 'montaña', 'ciudad'},
                'trabajo': {'trabajo', 'empleo', 'oficina', 'jefe', 'compañero', 'reunión', 'proyecto', 'deadline', 'cliente', 'empresa'},
                'estudio': {'estudio', 'universidad', 'colegio', 'examen', 'tarea', 'profesor', 'clase', 'aprender', 'educación', 'curso'}
            }
            topic_counts = {topic: 0 for topic in topics_keywords.keys()}
            topic_messages = {topic: [] for topic in topics_keywords.keys()}
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    text_lower = message.text.lower()
                    for topic, keywords in topics_keywords.items():
                        if any(keyword in text_lower for keyword in keywords):
                            topic_counts[topic] += 1
                            topic_messages[topic].append({
                                'id': message.id,
                                'date': message.date.isoformat(),
                                'text': message.text[:200] + '...' if len(message.text) > 200 else message.text
                            })
            sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
            return {
                'topic_counts': dict(sorted_topics),
                'topic_messages': topic_messages,
                'most_common_topics': [topic for topic, count in sorted_topics[:5] if count > 0]
            }
        except Exception as e:
            logger.error(f"Error analizando temas: {e}")
            return None

    # --- Reportes ---
    def generate_detailed_message_report(self, data):
        """Generar reporte detallado de mensajes"""
        report = ""
        if data.get('full_messages'):
            messages = data['full_messages']
            report += f"""
DETALLE COMPLETO DE MENSAJES:
=============================
Total de mensajes analizados: {len(messages)}
ÚLTIMOS 20 MENSAJES:
-------------------
"""
            for i, msg in enumerate(messages[:20], 1):
                report += f"{i}. 📅 {msg['date']}\n"
                if msg['text']:
                    text_preview = msg['text'][:150] + '...' if len(msg['text']) > 150 else msg['text']
                    report += f"   💬 {text_preview}\n"
                if msg['media_type'] != 'text':
                    report += f"   📎 {msg['media_type']}\n"
                if msg.get('urls'):
                    report += f"   🔗 {len(msg['urls'])} enlace(s)\n"
                report += "   " + "-" * 50 + "\n"

        if data.get('word_analysis'):
            word_data = data['word_analysis']
            report += f"""
ANÁLISIS DE VOCABULARIO:
========================
Palabras únicas utilizadas: {word_data.get('total_unique_words', 0)}
PALABRAS MÁS FRECUENTES (Top 25):
"""
            for word, count in word_data.get('most_common_words', [])[:25]:
                report += f"- '{word}': {count} veces\n"

        if data.get('message_categories'):
            categories = data['message_categories']
            stats = categories.get('stats', {})
            report += f"""
CATEGORÍAS DE MENSAJES:
======================
📝 Solo texto: {stats.get('text_only_count', 0)}
🔗 Con enlaces: {stats.get('with_links_count', 0)}
📎 Con medios: {stats.get('with_media_count', 0)}
❓ Preguntas: {stats.get('questions_count', 0)}
❗ Exclamaciones: {stats.get('exclamations_count', 0)}
📏 Mensajes largos (>200 chars): {stats.get('long_messages_count', 0)}
🔤 Mensajes cortos (<50 chars): {stats.get('short_messages_count', 0)}
"""
        if data.get('conversation_topics'):
            topics = data['conversation_topics']
            report += f"""
TEMAS DE CONVERSACIÓN:
=====================
Temas principales identificados: {', '.join(topics.get('most_common_topics', []))}
FRECUENCIA DE TEMAS:
"""
            for topic, count in topics.get('topic_counts', {}).items():
                if count > 0:
                    report += f"- {topic}: {count} mensajes\n"
        return report

    # --- Extracción de emails ---
    def extract_emails_from_text(self, text):
        """Extraer emails de un texto usando regex"""
        if not text:
            return []
        EMAIL_REGEX = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        return re.findall(EMAIL_REGEX, text)

    async def extract_emails_from_entity(self, entity, limit=1000):
        """Extraer emails de los mensajes de una entidad (usuario/canal)"""
        emails_data = []
        try:
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    emails = self.extract_emails_from_text(message.text)
                    for email in emails:
                        email_info = {
                            'email': email,
                            'chat': getattr(entity, 'title', str(getattr(entity, 'username', entity.id))),
                            'date': message.date.isoformat(),
                            'message_id': message.id,
                            'sender_id': message.sender_id
                        }
                        emails_data.append(email_info)
                        print(f"📧 Email encontrado: {email}")
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Error escaneando entidad {entity}: {e}")
        return emails_data

    async def save_emails_to_csv(self, emails_data, filename='emails_extraidos.csv'):
        """Guardar emails en CSV o JSON (si no hay pandas)"""
        try:
            import pandas as pd
            df = pd.DataFrame(emails_data)
            df.to_csv(filename, index=False, encoding='utf-8')
            print(f"💾 Emails guardados en: {filename}")
        except ImportError:
            json_filename = filename.replace('.csv', '.json')
            with open(json_filename, 'w', encoding='utf-8') as f:
                json.dump(emails_data, f, indent=2, ensure_ascii=False)
            print(f"⚠️ Pandas no disponible. Emails guardados en: {json_filename}")
        except Exception as e:
            logger.error(f"Error guardando resultados: {e}")

    # --- Análisis avanzados ---
    async def get_message_history_stats(self, username, limit=1000):
        """Obtiene estadísticas del historial de mensajes"""
        try:
            entity = await self.client.get_entity(username)
            stats = {
                'total_messages': 0,
                'photos_count': 0,
                'videos_count': 0,
                'documents_count': 0,
                'audio_count': 0,
                'first_message_date': None,
                'last_message_date': None
            }
            async for message in self.client.iter_messages(entity, limit=limit):
                stats['total_messages'] += 1
                if message.photo:
                    stats['photos_count'] += 1
                elif message.video:
                    stats['videos_count'] += 1
                elif message.document:
                    stats['documents_count'] += 1
                elif message.audio:
                    stats['audio_count'] += 1
                if stats['first_message_date'] is None:
                    stats['first_message_date'] = message.date.isoformat()
                stats['last_message_date'] = message.date.isoformat()
            return stats
        except Exception as e:
            logger.error(f"Error obteniendo estadísticas: {e}")
            return None

    async def analyze_message_patterns(self, username, limit=1000):
        """Analizar patrones de comportamiento en mensajes - VERSIÓN MEJORADA"""
        try:
            entity = await self.client.get_entity(username)
            patterns = {
                'activity_hours': Counter(),
                'activity_days': Counter(),
                'activity_months': Counter(),
                'message_lengths': [],
                'common_words': Counter(),
                'media_frequency': Counter(),
                'reply_frequency': 0,
                'forward_frequency': 0,
                'total_messages_processed': 0,
                'messages_with_text': 0,
                'messages_with_dates': 0
            }
            logger.info(f"🔍 Analizando {limit} mensajes de {username}...")
            message_count = 0
            async for message in self.client.iter_messages(entity, limit=limit):
                message_count += 1
                patterns['total_messages_processed'] = message_count
                if message.date:
                    patterns['messages_with_dates'] += 1
                    try:
                        patterns['activity_hours'][message.date.hour] += 1
                        patterns['activity_days'][message.date.strftime('%A')] += 1
                        patterns['activity_months'][message.date.strftime('%B')] += 1
                    except Exception as time_error:
                        logger.debug(f"Error procesando fecha: {time_error}")
                if message.text:
                    patterns['messages_with_text'] += 1
                    text_length = len(message.text)
                    patterns['message_lengths'].append(text_length)
                    try:
                        words = re.findall(r'\b[a-zA-Záéíóúñ]+\b', message.text.lower())
                        common_stop_words = {
                            'el', 'la', 'de', 'que', 'y', 'en', 'un', 'es', 'se', 'no',
                            'te', 'lo', 'le', 'me', 'mi', 'tu', 'su', 'los', 'las', 'del'
                        }
                        filtered_words = [w for w in words if w not in common_stop_words and len(w) > 2]
                        patterns['common_words'].update(filtered_words)
                    except Exception as text_error:
                        logger.debug(f"Error procesando texto: {text_error}")
                if message.media:
                    try:
                        media_type = type(message.media).__name__
                        patterns['media_frequency'][media_type] += 1
                    except Exception as media_error:
                        logger.debug(f"Error procesando medio: {media_error}")
                if hasattr(message, 'reply_to') and message.reply_to:
                    patterns['reply_frequency'] += 1
                if hasattr(message, 'fwd_from') and message.fwd_from:
                    patterns['forward_frequency'] += 1
                if message_count % 100 == 0:
                    logger.info(f"📨 Procesados {message_count}/{limit} mensajes...")

            patterns['total_messages_analyzed'] = limit
            if patterns['message_lengths']:
                patterns['avg_message_length'] = sum(patterns['message_lengths']) / len(patterns['message_lengths'])
                patterns['max_message_length'] = max(patterns['message_lengths'])
                patterns['min_message_length'] = min(patterns['message_lengths'])
            else:
                patterns['avg_message_length'] = 0
                patterns['max_message_length'] = 0
                patterns['min_message_length'] = 0

            if patterns['activity_hours']:
                most_common_hour = patterns['activity_hours'].most_common(1)
                patterns['most_active_hour'] = most_common_hour[0] if most_common_hour else None
            else:
                patterns['most_active_hour'] = None

            patterns['most_active_day'] = patterns['activity_days'].most_common(1)[0] if patterns['activity_days'] else None
            patterns['most_active_month'] = patterns['activity_months'].most_common(1)[0] if patterns['activity_months'] else None
            patterns['most_common_words'] = patterns['common_words'].most_common(15)
            patterns['total_media'] = sum(patterns['media_frequency'].values())
            patterns['media_percentage'] = (patterns['total_media'] / patterns['total_messages_processed']) * 100 if patterns['total_messages_processed'] > 0 else 0
            patterns['text_percentage'] = (patterns['messages_with_text'] / patterns['total_messages_processed']) * 100 if patterns['total_messages_processed'] > 0 else 0
            patterns['reply_percentage'] = (patterns['reply_frequency'] / patterns['total_messages_processed']) * 100 if patterns['total_messages_processed'] > 0 else 0
            patterns['forward_percentage'] = (patterns['forward_frequency'] / patterns['total_messages_processed']) * 100 if patterns['total_messages_processed'] > 0 else 0

            logger.info(f"✅ Análisis completado: {patterns['total_messages_processed']} mensajes procesados")
            return patterns
        except Exception as e:
            logger.error(f"❌ Error analizando patrones: {e}")
            return None

    async def get_contact_network(self, username, max_contacts=50):
        """Mapear la red de contactos del usuario"""
        try:
            entity = await self.client.get_entity(username)
            network = {'common_groups': [], 'frequent_contacts': [], 'mutual_contacts': []}
            async for dialog in self.client.iter_dialogs(limit=100):
                if dialog.is_group:
                    try:
                        participants = await self.client.get_participants(dialog.entity, limit=100)
                        participant_ids = [p.id for p in participants]
                        if entity.id in participant_ids:
                            network['common_groups'].append({
                                'name': dialog.name,
                                'id': dialog.id,
                                'participants_count': len(participants),
                                'type': 'group' if dialog.is_group else 'channel'
                            })
                    except:
                        continue
            return network
        except Exception as e:
            logger.error(f"Error mapeando red de contactos: {e}")
            return None

    async def search_username_across_platforms(self, username):
        """Buscar el username en otras plataformas (OSINT externo)"""
        platforms = {
            'instagram': f'https://www.instagram.com/{username}',
            'twitter': f'https://twitter.com/{username}',
            'github': f'https://github.com/{username}',
            'facebook': f'https://facebook.com/{username}',
            'tiktok': f'https://tiktok.com/@{username}',
            'youtube': f'https://youtube.com/@{username}',
            'reddit': f'https://reddit.com/user/{username}'
        }
        results = {}
        for platform, url in platforms.items():
            try:
                response = requests.get(url, timeout=10)
                results[platform] = {'url': url, 'exists': response.status_code == 200, 'status_code': response.status_code}
                time.sleep(1)
            except Exception as e:
                results[platform] = {'url': url, 'exists': False, 'error': str(e)}
        return results

    async def geolocation_analysis(self, username, limit=500):
        """Analizar posibles ubicaciones geográficas"""
        try:
            entity = await self.client.get_entity(username)
            locations = []
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    location_patterns = [
                        r'\b(calle|avenida|av\.|ciudad|pueblo|barrio|plaza)\s+\w+',
                        r'\b(madrid|barcelona|valencia|sevilla|bilbao|málaga|zaragoza|murcia|palma|granada)\b',
                        r'\b(\d{5})\b',
                        r'\b(españa|espana|spain)\b',
                        r'\b(méxico|mexico|argentina|colombia|chile|perú|peru|venezuela)\b'
                    ]
                    for pattern in location_patterns:
                        matches = re.findall(pattern, message.text, re.IGNORECASE)
                        if matches:
                            locations.extend(matches)
            return {
                'mentioned_locations': list(set(locations)),
                'total_mentions': len(locations),
                'unique_locations': len(set(locations))
            }
        except Exception as e:
            logger.error(f"Error en análisis geográfico: {e}")
            return None

    async def sentiment_analysis(self, username, limit=500):
        """Análisis básico de sentimiento en mensajes"""
        try:
            entity = await self.client.get_entity(username)
            positive_words = {'bueno', 'genial', 'excelente', 'fantástico', 'maravilloso', 'feliz', 'contento', 'alegre', 'amo', 'encanta', 'increíble'}
            negative_words = {'malo', 'terrible', 'horrible', 'triste', 'enojado', 'molesto', 'frustrado', 'odio', 'asco', 'aburrido', 'cansado'}
            sentiment_stats = {'positive_count': 0, 'negative_count': 0, 'neutral_count': 0, 'total_messages': 0}
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    sentiment_stats['total_messages'] += 1
                    text_lower = message.text.lower()
                    positive_matches = sum(1 for word in positive_words if word in text_lower)
                    negative_matches = sum(1 for word in negative_words if word in text_lower)
                    if positive_matches > negative_matches:
                        sentiment_stats['positive_count'] += 1
                    elif negative_matches > positive_matches:
                        sentiment_stats['negative_count'] += 1
                    else:
                        sentiment_stats['neutral_count'] += 1
            if sentiment_stats['total_messages'] > 0:
                sentiment_stats['positive_percentage'] = (sentiment_stats['positive_count'] / sentiment_stats['total_messages']) * 100
                sentiment_stats['negative_percentage'] = (sentiment_stats['negative_count'] / sentiment_stats['total_messages']) * 100
                sentiment_stats['neutral_percentage'] = (sentiment_stats['neutral_count'] / sentiment_stats['total_messages']) * 100
            return sentiment_stats
        except Exception as e:
            logger.error(f"Error en análisis de sentimiento: {e}")
            return None

    async def timeline_analysis(self, username, limit=1000):
        """Crear línea de tiempo de actividad"""
        try:
            entity = await self.client.get_entity(username)
            timeline = []
            async for message in self.client.iter_messages(entity, limit=limit):
                timeline_event = {
                    'date': message.date.isoformat(),
                    'type': 'message',
                    'content_preview': message.text[:100] + '...' if message.text and len(message.text) > 100 else message.text,
                    'media_type': type(message.media).__name__ if message.media else 'text'
                }
                timeline.append(timeline_event)
            return sorted(timeline, key=lambda x: x['date'], reverse=True)
        except Exception as e:
            logger.error(f"Error en análisis de timeline: {e}")
            return []

    # --- Historial y grupos ---
    async def search_public_groups(self, username):
        """Buscar en grupos públicos"""
        try:
            groups = []
            async for dialog in self.client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    try:
                        participants = await self.client.get_participants(dialog.entity)
                        for participant in participants:
                            if (participant.username and participant.username.lower() == username.lower()) or \
                               hasattr(participant, 'id') and str(participant.id) == username:
                                groups.append({
                                    'name': dialog.name,
                                    'id': dialog.id,
                                    'type': 'group' if dialog.is_group else 'channel',
                                    'participants_count': len(participants)
                                })
                    except:
                        continue
            return groups
        except Exception as e:
            logger.error(f"Error buscando en grupos: {e}")
            return []

    async def get_old_usernames(self, target_user=None):
        """Obtiene el historial de los nombres y usernames del usuario."""
        try:
            if target_user:
                entity = await self.client.get_entity(target_user)
                user_id = entity.id
            else:
                me = await self.client.get_me()
                user_id = me.id
            try:
                history = await self.client(functions.account.GetUsernameHistoryRequest(user_id=user_id))
                usernames = []
                for item in history:
                    usernames.append({
                        "old_username": item.username,
                        "active": getattr(item, 'active', False),
                        "edited_date": str(getattr(item, 'edit_date', 'N/A'))
                    })
                return usernames
            except Exception as e:
                logger.warning(f"No se pudo obtener historial de usernames: {e}")
                return []
        except Exception as e:
            logger.error(f"Error obteniendo historial de usernames: {e}")
            return []

    async def get_created_channels(self, target_user=None):
        """Encuentra canales o grupos que el usuario creó."""
        created = []
        try:
            async for dialog in self.client.iter_dialogs():
                entity = dialog.entity
                if isinstance(entity, Channel):
                    if target_user:
                        target_entity = await self.client.get_entity(target_user)
                        if hasattr(entity, 'creator') and entity.creator and entity.creator.id == target_entity.id:
                            created.append({
                                "name": entity.title,
                                "id": entity.id,
                                "type": "channel" if entity.broadcast else "group",
                                "participants": getattr(entity, 'participants_count', 'N/A'),
                                "username": getattr(entity, 'username', 'N/A')
                            })
                    else:
                        created.append({
                            "name": entity.title,
                            "id": entity.id,
                            "type": "channel" if entity.broadcast else "group",
                            "participants": getattr(entity, 'participants_count', 'N/A'),
                            "username": getattr(entity, 'username', 'N/A')
                        })
            return created
        except Exception as e:
            logger.error(f"Error buscando canales creados: {e}")
            return []

    # --- Nuevas funciones añadidas ---
    
    async def get_deleted_account_info(self, phone_number):
        """Buscar información de cuentas eliminadas por número de teléfono"""
        try:
            # Intentar encontrar el usuario por número de teléfono
            entity = await self.client.get_entity(phone_number)
            if entity:
                return await self.get_user_info(phone_number)
            return None
        except Exception as e:
            logger.info(f"Cuenta posiblemente eliminada o no encontrada: {e}")
            return {"status": "deleted_or_not_found", "phone": phone_number}

    async def analyze_group_activity(self, group_username, user_filter=None):
        """Analizar actividad en grupos específicos"""
        try:
            entity = await self.client.get_entity(group_username)
            activity_data = {
                'total_messages': 0,
                'active_users': Counter(),
                'message_frequency': Counter(),
                'top_posters': [],
                'recent_activity': []
            }
            
            async for message in self.client.iter_messages(entity, limit=1000):
                activity_data['total_messages'] += 1
                
                if message.sender_id:
                    activity_data['active_users'][message.sender_id] += 1
                
                if message.date:
                    activity_data['message_frequency'][message.date.strftime('%Y-%m-%d')] += 1
                
                # Solo mantener los últimos 50 mensajes para análisis detallado
                if len(activity_data['recent_activity']) < 50:
                    activity_data['recent_activity'].append({
                        'date': message.date.isoformat(),
                        'sender_id': message.sender_id,
                        'text': message.text[:200] if message.text else '',
                        'media_type': type(message.media).__name__ if message.media else 'text'
                    })
            
            # Obtener información de los usuarios más activos
            for user_id, count in activity_data['active_users'].most_common(10):
                try:
                    user_entity = await self.client.get_entity(user_id)
                    activity_data['top_posters'].append({
                        'user_id': user_id,
                        'username': getattr(user_entity, 'username', 'N/A'),
                        'first_name': getattr(user_entity, 'first_name', 'N/A'),
                        'last_name': getattr(user_entity, 'last_name', 'N/A'),
                        'message_count': count
                    })
                except:
                    activity_data['top_posters'].append({
                        'user_id': user_id,
                        'username': 'N/A',
                        'first_name': 'N/A',
                        'last_name': 'N/A',
                        'message_count': count
                    })
            
            return activity_data
        except Exception as e:
            logger.error(f"Error analizando actividad del grupo: {e}")
            return None

    async def extract_phone_numbers(self, username, limit=500):
        """Extraer números de teléfono mencionados en mensajes"""
        try:
            entity = await self.client.get_entity(username)
            phone_numbers = []
            phone_patterns = [
                r'\+\d{1,3}[-.\s]?\d{1,14}',
                r'\d{3}[-.\s]?\d{3}[-.\s]?\d{4}',
                r'\(\d{3}\)\s*\d{3}[-.\s]?\d{4}'
            ]
            
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    for pattern in phone_patterns:
                        matches = re.findall(pattern, message.text)
                        for match in matches:
                            phone_info = {
                                'phone': match,
                                'date': message.date.isoformat(),
                                'message_id': message.id,
                                'context': message.text[:100] + '...' if len(message.text) > 100 else message.text
                            }
                            phone_numbers.append(phone_info)
            
            return phone_numbers
        except Exception as e:
            logger.error(f"Error extrayendo números de teléfono: {e}")
            return []

    async def get_user_connections_map(self, username):
        """Crear mapa de conexiones del usuario"""
        try:
            entity = await self.client.get_entity(username)
            connections = {
                'common_groups': [],
                'frequent_contacts': [],
                'mentioned_users': [],
                'interaction_network': {}
            }
            
            # Buscar grupos en común
            async for dialog in self.client.iter_dialogs(limit=100):
                if dialog.is_group or dialog.is_channel:
                    try:
                        participants = await self.client.get_participants(dialog.entity, limit=100)
                        participant_ids = [p.id for p in participants]
                        if entity.id in participant_ids:
                            group_info = {
                                'name': dialog.name,
                                'id': dialog.id,
                                'type': 'group' if dialog.is_group else 'channel',
                                'participants_count': len(participants),
                                'common_contacts': []
                            }
                            
                            # Encontrar contactos en común
                            for participant in participants[:20]:  # Limitar para no sobrecargar
                                if participant.id != entity.id:
                                    group_info['common_contacts'].append({
                                        'id': participant.id,
                                        'username': getattr(participant, 'username', 'N/A'),
                                        'first_name': getattr(participant, 'first_name', 'N/A'),
                                        'last_name': getattr(participant, 'last_name', 'N/A')
                                    })
                            
                            connections['common_groups'].append(group_info)
                    except:
                        continue
            
            return connections
        except Exception as e:
            logger.error(f"Error creando mapa de conexiones: {e}")
            return None

    async def monitor_user_activity(self, username, duration_minutes=60, check_interval=5):
        """Monitorear actividad del usuario en tiempo real"""
        try:
            entity = await self.client.get_entity(username)
            start_time = datetime.now()
            end_time = start_time + timedelta(minutes=duration_minutes)
            activity_log = []
            
            print(f"🔍 Monitoreando actividad de {username} por {duration_minutes} minutos...")
            
            while datetime.now() < end_time:
                try:
                    # Obtener el último mensaje
                    messages = await self.client.get_messages(entity, limit=1)
                    if messages:
                        last_message = messages[0]
                        
                        # Verificar si es un mensaje nuevo
                        if not activity_log or last_message.id != activity_log[-1]['message_id']:
                            activity_log.append({
                                'timestamp': datetime.now().isoformat(),
                                'message_id': last_message.id,
                                'text': last_message.text[:200] if last_message.text else '',
                                'media_type': type(last_message.media).__name__ if last_message.media else 'text',
                                'date': last_message.date.isoformat()
                            })
                            print(f"📨 Nueva actividad detectada: {last_message.date}")
                    
                    await asyncio.sleep(check_interval * 60)  # Convertir a segundos
                    
                except Exception as e:
                    logger.error(f"Error durante monitoreo: {e}")
                    await asyncio.sleep(30)  # Esperar 30 segundos antes de reintentar
            
            return {
                'monitoring_duration': f"{duration_minutes} minutos",
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'activity_detected': len(activity_log),
                'activity_log': activity_log
            }
            
        except Exception as e:
            logger.error(f"Error iniciando monitoreo: {e}")
            return None

    async def analyze_message_style(self, username, limit=500):
        """Analizar estilo de escritura y patrones lingüísticos"""
        try:
            entity = await self.client.get_entity(username)
            style_analysis = {
                'avg_message_length': 0,
                'message_lengths': [],
                'punctuation_usage': Counter(),
                'emoticon_usage': Counter(),
                'capitalization_patterns': {},
                'common_phrases': Counter(),
                'writing_style_metrics': {}
            }
            
            messages_processed = 0
            total_length = 0
            
            async for message in self.client.iter_messages(entity, limit=limit):
                if message.text:
                    messages_processed += 1
                    text = message.text
                    text_length = len(text)
                    
                    total_length += text_length
                    style_analysis['message_lengths'].append(text_length)
                    
                    # Análisis de puntuación
                    style_analysis['punctuation_usage']['periods'] += text.count('.')
                    style_analysis['punctuation_usage']['commas'] += text.count(',')
                    style_analysis['punctuation_usage']['exclamations'] += text.count('!')
                    style_analysis['punctuation_usage']['questions'] += text.count('?')
                    
                    # Análisis de emoticonos
                    emoticons = re.findall(r'[:;][\'`\-]?[\)\(PD\/\\]', text)
                    style_analysis['emoticon_usage'].update(emoticons)
                    
                    # Patrones de capitalización
                    words = re.findall(r'\b[A-Za-záéíóúñÁÉÍÓÚÑ]+\b', text)
                    if words:
                        capitalized = sum(1 for w in words if w[0].isupper())
                        style_analysis['capitalization_patterns']['total_words'] = len(words)
                        style_analysis['capitalization_patterns']['capitalized_words'] = capitalized
                        style_analysis['capitalization_patterns']['capitalization_rate'] = (capitalized / len(words)) * 100
                    
                    # Frases comunes (bigramas)
                    words_lower = [w.lower() for w in words if len(w) > 2]
                    bigrams = [f"{words_lower[i]} {words_lower[i+1]}" for i in range(len(words_lower)-1)]
                    style_analysis['common_phrases'].update(bigrams)
            
            if messages_processed > 0:
                style_analysis['avg_message_length'] = total_length / messages_processed
                style_analysis['writing_style_metrics']['total_messages_analyzed'] = messages_processed
                style_analysis['writing_style_metrics']['total_characters'] = total_length
                style_analysis['writing_style_metrics']['chars_per_message'] = style_analysis['avg_message_length']
            
            return style_analysis
            
        except Exception as e:
            logger.error(f"Error analizando estilo de escritura: {e}")
            return None

    # --- Reportes completos ---
    async def get_full_user_info(self, username_or_phone):
        """Obtener información completa del usuario"""
        logger.info(f"Buscando información para: {username_or_phone}")
        user_info = await self.get_user_info(username_or_phone)
        if not user_info:
            return None
        public_groups = await self.search_public_groups(username_or_phone)
        old_usernames = await self.get_old_usernames(username_or_phone)
        created_channels = await self.get_created_channels(username_or_phone)
        message_stats = await self.get_message_history_stats(username_or_phone)
        complete_info = {
            'user_info': user_info,
            'public_groups': public_groups,
            'old_usernames': old_usernames,
            'created_channels': created_channels,
            'message_statistics': message_stats,
            'search_timestamp': datetime.now().isoformat()
        }
        return complete_info

    async def get_complete_osint_report(self, username_or_phone):
        """Generar reporte OSINT completo con todas las funcionalidades"""
        logger.info(f"🚀 Iniciando análisis OSINT completo para: {username_or_phone}")
        user_info = await self.get_user_info(username_or_phone)
        if not user_info:
            return None
        print("🔍 Recopilando información básica... ✅")

        tasks = [
            self.search_public_groups(username_or_phone),
            self.get_old_usernames(username_or_phone),
            self.get_created_channels(username_or_phone),
            self.get_message_history_stats(username_or_phone),
            self.analyze_message_patterns(username_or_phone),
            self.get_contact_network(username_or_phone),
            self.search_username_across_platforms(user_info.get('username', '')),
            self.geolocation_analysis(username_or_phone),
            self.sentiment_analysis(username_or_phone),
            self.timeline_analysis(username_or_phone)
        ]
        print("🔄 Ejecutando análisis avanzados...")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        complete_report = {
            'user_info': user_info,
            'public_groups': results[0] if not isinstance(results[0], Exception) else [],
            'old_usernames': results[1] if not isinstance(results[1], Exception) else [],
            'created_channels': results[2] if not isinstance(results[2], Exception) else [],
            'message_statistics': results[3] if not isinstance(results[3], Exception) else {},
            'behavior_patterns': results[4] if not isinstance(results[4], Exception) else {},
            'contact_network': results[5] if not isinstance(results[5], Exception) else {},
            'cross_platform_presence': results[6] if not isinstance(results[6], Exception) else {},
            'geolocation_analysis': results[7] if not isinstance(results[7], Exception) else {},
            'sentiment_analysis': results[8] if not isinstance(results[8], Exception) else {},
            'activity_timeline': results[9] if not isinstance(results[9], Exception) else [],
            'search_timestamp': datetime.now().isoformat(),
            'report_version': '2.0'
        }
        print("✅ Análisis completo finalizado")
        return complete_report

    async def get_enhanced_osint_report(self, username_or_phone):
        """Generar reporte OSINT mejorado con análisis detallado de mensajes"""
        logger.info(f"🚀 Iniciando análisis OSINT MEJORADO para: {username_or_phone}")
        user_info = await self.get_user_info(username_or_phone)
        if not user_info:
            return None
        print("🔍 Recopilando información básica... ✅")

        tasks = [
            self.search_public_groups(username_or_phone),
            self.get_old_usernames(username_or_phone),
            self.get_created_channels(username_or_phone),
            self.get_message_history_stats(username_or_phone),
            self.analyze_message_patterns(username_or_phone),
            self.get_contact_network(username_or_phone),
            self.search_username_across_platforms(user_info.get('username', '')),
            self.geolocation_analysis(username_or_phone),
            self.sentiment_analysis(username_or_phone),
            self.timeline_analysis(username_or_phone),
            self.get_full_message_history(username_or_phone, 200),
            self.get_all_words_used(username_or_phone, 200),
            self.get_message_categories(username_or_phone, 200),
            self.get_conversation_topics(username_or_phone, 200)
        ]
        print("🔄 Ejecutando análisis avanzados y detallados...")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enhanced_report = {
            'user_info': user_info,
            'public_groups': results[0] if not isinstance(results[0], Exception) else [],
            'old_usernames': results[1] if not isinstance(results[1], Exception) else [],
            'created_channels': results[2] if not isinstance(results[2], Exception) else [],
            'message_statistics': results[3] if not isinstance(results[3], Exception) else {},
            'behavior_patterns': results[4] if not isinstance(results[4], Exception) else {},
            'contact_network': results[5] if not isinstance(results[5], Exception) else {},
            'cross_platform_presence': results[6] if not isinstance(results[6], Exception) else {},
            'geolocation_analysis': results[7] if not isinstance(results[7], Exception) else {},
            'sentiment_analysis': results[8] if not isinstance(results[8], Exception) else {},
            'activity_timeline': results[9] if not isinstance(results[9], Exception) else [],
            'full_messages': results[10] if not isinstance(results[10], Exception) else [],
            'word_analysis': results[11] if not isinstance(results[11], Exception) else {},
            'message_categories': results[12] if not isinstance(results[12], Exception) else {},
            'conversation_topics': results[13] if not isinstance(results[13], Exception) else {},
            'search_timestamp': datetime.now().isoformat(),
            'report_version': '3.0'
        }
        print("✅ Análisis completo y detallado finalizado")
        return enhanced_report

    async def get_premium_osint_report(self, username_or_phone):
        """Reporte OSINT premium con todas las funciones nuevas"""
        logger.info(f"🚀 Iniciando análisis OSINT PREMIUM para: {username_or_phone}")
        user_info = await self.get_user_info(username_or_phone)
        if not user_info:
            return None
        
        print("🔍 Recopilando información básica... ✅")
        
        # Ejecutar todas las funciones disponibles
        tasks = [
            self.search_public_groups(username_or_phone),
            self.get_old_usernames(username_or_phone),
            self.get_created_channels(username_or_phone),
            self.get_message_history_stats(username_or_phone),
            self.analyze_message_patterns(username_or_phone),
            self.get_contact_network(username_or_phone),
            self.search_username_across_platforms(user_info.get('username', '')),
            self.geolocation_analysis(username_or_phone),
            self.sentiment_analysis(username_or_phone),
            self.timeline_analysis(username_or_phone),
            self.get_full_message_history(username_or_phone, 300),
            self.get_all_words_used(username_or_phone, 300),
            self.get_message_categories(username_or_phone, 300),
            self.get_conversation_topics(username_or_phone, 300),
            self.extract_phone_numbers(username_or_phone, 200),
            self.get_user_connections_map(username_or_phone),
            self.analyze_message_style(username_or_phone, 200)
        ]
        
        print("🔄 Ejecutando análisis premium...")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        premium_report = {
            'user_info': user_info,
            'public_groups': results[0] if not isinstance(results[0], Exception) else [],
            'old_usernames': results[1] if not isinstance(results[1], Exception) else [],
            'created_channels': results[2] if not isinstance(results[2], Exception) else [],
            'message_statistics': results[3] if not isinstance(results[3], Exception) else {},
            'behavior_patterns': results[4] if not isinstance(results[4], Exception) else {},
            'contact_network': results[5] if not isinstance(results[5], Exception) else {},
            'cross_platform_presence': results[6] if not isinstance(results[6], Exception) else {},
            'geolocation_analysis': results[7] if not isinstance(results[7], Exception) else {},
            'sentiment_analysis': results[8] if not isinstance(results[8], Exception) else {},
            'activity_timeline': results[9] if not isinstance(results[9], Exception) else [],
            'full_messages': results[10] if not isinstance(results[10], Exception) else [],
            'word_analysis': results[11] if not isinstance(results[11], Exception) else {},
            'message_categories': results[12] if not isinstance(results[12], Exception) else {},
            'conversation_topics': results[13] if not isinstance(results[13], Exception) else {},
            'extracted_phones': results[14] if not isinstance(results[14], Exception) else [],
            'connections_map': results[15] if not isinstance(results[15], Exception) else {},
            'writing_style_analysis': results[16] if not isinstance(results[16], Exception) else {},
            'search_timestamp': datetime.now().isoformat(),
            'report_version': 'PREMIUM'
        }
        
        print("✅ Análisis premium finalizado")
        return premium_report

    def generate_report(self, data):
        """Generar reporte legible"""
        user = data['user_info']
        report = f"""
=== REPORTE OSINT TELEGRAM AVANZADO ===
Fecha: {data['search_timestamp']}
INFORMACIÓN DEL USUARIO:
------------------------
ID: {user['id']}
Username: @{user['username']}
Nombre: {user['first_name']} {user['last_name']}
Teléfono: {user['phone']}
Verificado: {user['verified']}
Premium: {user['premium']}
Bot: {user['bot']}
Biografía: {user['bio']}
ESTADO:
-------
Última vez: {user['last_seen']}
Estado: {user['status']}
Centro de Datos: {user['dc_id']}
Idioma: {user['lang_code']}
SEGURIDAD:
----------
Restringido: {user['restricted']}
Scam: {user['scam']}
Fake: {user['fake']}
HISTORIAL DE USERNAMES ({len(data['old_usernames'])}):
-----------------------
"""
        for username in data['old_usernames']:
            status = "ACTIVO" if username['active'] else "ANTIGUO"
            report += f"- @{username['old_username']} ({status}) - {username['edited_date']}\n"
        report += f"""
CANALES CREADOS ({len(data['created_channels'])}):
-----------------
"""
        for channel in data['created_channels']:
            report += f"- {channel['name']} ({channel['type']}) - {channel['participants']} miembros\n"
            if channel['username'] != 'N/A':
                report += f"  @{channel['username']}\n"
        report += f"""
GRUPOS PÚBLICOS ({len(data['public_groups'])}):
-----------------
"""
        for group in data['public_groups']:
            report += f"- {group['name']} ({group['type']}) - {group['participants_count']} miembros\n"
        if data.get('message_statistics'):
            stats = data['message_statistics']
            report += f"""
ESTADÍSTICAS DE MENSAJES:
-------------------------
Total mensajes analizados: {stats['total_messages']}
Fotos: {stats['photos_count']}
Videos: {stats['videos_count']}
Documentos: {stats['documents_count']}
Audios: {stats['audio_count']}
Primer mensaje: {stats['first_message_date']}
Último mensaje: {stats['last_message_date']}
"""
        if user['photo']:
            report += f"\n📸 Foto de perfil guardada en: {user['photo']}"
        return report

    def generate_advanced_report(self, data):
        """Generar reporte avanzado con toda la información nueva - VERSIÓN MEJORADA"""
        report = self.generate_report(data)
        if data.get('behavior_patterns'):
            patterns = data['behavior_patterns']
            report += f"""
ANÁLISIS DE COMPORTAMIENTO DETALLADO:
-------------------------------------
Mensajes procesados: {patterns.get('total_messages_processed', 0)}
Mensajes con texto: {patterns.get('messages_with_text', 0)} ({patterns.get('text_percentage', 0):.1f}%)
Mensajes con medios: {patterns.get('total_media', 0)} ({patterns.get('media_percentage', 0):.1f}%)
Mensajes como respuesta: {patterns.get('reply_frequency', 0)} ({patterns.get('reply_percentage', 0):.1f}%)
Mensajes reenviados: {patterns.get('forward_frequency', 0)} ({patterns.get('forward_percentage', 0):.1f}%)
HORARIO DE ACTIVIDAD:
"""
            if patterns.get('most_active_hour'):
                hour, count = patterns['most_active_hour']
                report += f"Hora más activa: {hour:02d}:00 ({count} mensajes)\n"
            else:
                report += f"Hora más activa: No disponible\n"
            if patterns.get('most_active_day'):
                day, count = patterns['most_active_day']
                report += f"Día más activo: {day} ({count} mensajes)\n"
            if patterns.get('most_active_month'):
                month, count = patterns['most_active_month']
                report += f"Mes más activo: {month} ({count} mensajes)\n"
            report += f"""
ESTADÍSTICAS DE TEXTO:
----------------------
Longitud promedio: {patterns.get('avg_message_length', 0):.1f} caracteres
Longitud máxima: {patterns.get('max_message_length', 0)} caracteres
Longitud mínima: {patterns.get('min_message_length', 0)} caracteres
TIPOS DE MEDIOS ENCONTRADOS:
"""
            for media_type, count in patterns.get('media_frequency', {}).items():
                report += f"- {media_type}: {count}\n"
            report += f"""
PALABRAS MÁS USADAS (Top 10):
"""
            for word, count in patterns.get('most_common_words', [])[:10]:
                report += f"- '{word}': {count} veces\n"

        if data.get('cross_platform_presence'):
            platforms = data['cross_platform_presence']
            report += f"""
PRESENCIA EN OTRAS PLATAFORMAS:
-------------------------------
"""
            found_count = 0
            for platform, info in platforms.items():
                status = "✅ ENCONTRADO" if info.get('exists') else "❌ NO ENCONTRADO"
                if info.get('exists'):
                    found_count += 1
                report += f"- {platform.capitalize()}: {status}"
                if info.get('exists'):
                    report += f" ({info['url']})"
                report += "\n"
            report += f"Total plataformas encontradas: {found_count}/{len(platforms)}\n"

        if data.get('sentiment_analysis'):
            sentiment = data['sentiment_analysis']
            report += f"""
ANÁLISIS DE SENTIMIENTO:
------------------------
Positivo: {sentiment.get('positive_percentage', 0):.1f}% ({sentiment.get('positive_count', 0)} mensajes)
Negativo: {sentiment.get('negative_percentage', 0):.1f}% ({sentiment.get('negative_count', 0)} mensajes)
Neutral: {sentiment.get('neutral_percentage', 0):.1f}% ({sentiment.get('neutral_count', 0)} mensajes)
Total mensajes analizados: {sentiment.get('total_messages', 0)}
"""
        if data.get('geolocation_analysis') and data['geolocation_analysis']['mentioned_locations']:
            geo = data['geolocation_analysis']
            report += f"""
ANÁLISIS GEOGRÁFICO:
--------------------
Ubicaciones mencionadas: {', '.join(geo['mentioned_locations'][:10])}
Total menciones: {geo['total_mentions']}
Ubicaciones únicas: {geo['unique_locations']}
"""
        else:
            report += f"""
ANÁLISIS GEOGRÁFICO:
--------------------
No se encontraron menciones de ubicaciones en los mensajes analizados.
"""
        if data.get('activity_timeline'):
            timeline = data['activity_timeline']
            report += f"""
LÍNEA DE TIEMPO RECIENTE (Últimos 10 eventos):
-----------------------------------------------
"""
            for event in timeline[:10]:
                report += f"- {event['date']}: {event['type']}"
                if event['content_preview']:
                    report += f" - {event['content_preview']}"
                report += "\n"
        if data.get('contact_network') and data['contact_network'].get('common_groups'):
            network = data['contact_network']
            report += f"""
RED DE CONTACTOS:
-----------------
Grupos en común: {len(network['common_groups'])}
"""
            for group in network['common_groups'][:5]:
                report += f"- {group['name']} ({group['type']}) - {group['participants_count']} miembros\n"
        
        # Nuevas secciones para funciones premium
        if data.get('extracted_phones'):
            phones = data['extracted_phones']
            report += f"""
NÚMEROS DE TELÉFONO ENCONTRADOS ({len(phones)}):
---------------------------------
"""
            for phone in phones[:5]:
                report += f"- {phone['phone']} (Contexto: {phone['context']})\n"
            if len(phones) > 5:
                report += f"... y {len(phones) - 5} más\n"
        
        if data.get('writing_style_analysis'):
            style = data['writing_style_analysis']
            report += f"""
ANÁLISIS DE ESTILO DE ESCRITURA:
--------------------------------
Longitud promedio de mensajes: {style.get('avg_message_length', 0):.1f} caracteres
Mensajes analizados: {style.get('writing_style_metrics', {}).get('total_messages_analyzed', 0)}
Puntuación utilizada:
  • Puntos: {style.get('punctuation_usage', {}).get('periods', 0)}
  • Comas: {style.get('punctuation_usage', {}).get('commas', 0)}
  • Exclamaciones: {style.get('punctuation_usage', {}).get('exclamations', 0)}
  • Preguntas: {style.get('punctuation_usage', {}).get('questions', 0)}
"""
        return report

    def save_results(self, data, filename=None):
        """Guardar resultados en JSON"""
        if not filename:
            username = data['user_info'].get('username', 'unknown')
            filename = f"osint_results_{username}_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Resultados guardados en: {filename}")
        return filename

    async def safe_search(self, username, max_retries=3):
        """Búsqueda con manejo de errores y reintentos"""
        for attempt in range(max_retries):
            try:
                return await self.get_user_info(username)
            except Exception as e:
                logger.warning(f"Intento {attempt + 1} fallido: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
        return None

    async def recover_specific_photos(self, username, photo_limit=100):
        """Función específica para recuperar fotos antiguas"""
        print(f"🕵️ Iniciando recuperación de fotos para: {username}")
        print("⚠️ Esto puede tomar tiempo dependiendo de la cantidad de mensajes...")
        photos = await self.get_all_old_photos(username, limit=photo_limit)
        if photos:
            print(f"✅ Se recuperaron {len(photos)} fotos")
            for photo in photos[:5]:
                print(f"   📅 {photo['date']} - Guardada en: {photo['saved_at']}")
            if len(photos) > 5:
                print(f"   ... y {len(photos) - 5} más")
        else:
            print("❌ No se encontraron fotos para recuperar")
        return photos

    def cleanup_temp_files(self):
        """Limpiar archivos temporales"""
        import glob
        temp_files = glob.glob("temp_analysis_*") + glob.glob("deleted_photos/*") + glob.glob("recovered_photos_*/*")
        for file in temp_files:
            try:
                os.remove(file)
            except:
                pass


# --- Función principal ---
async def main():
    API_ID = API_CONFIG["api_id"]
    API_HASH = API_CONFIG["api_hash"]

    if API_ID == "TU_API_ID" or API_HASH == "TU_API_HASH":
        print("❌ ERROR: Debes configurar tus credenciales de API en config.py")
        print("📍 Obtén tus credenciales en: https://my.telegram.org/")
        return

    osint_tool = TelegramOSINT(API_ID, API_HASH)
    try:
        await osint_tool.start_client()
        print("""
  __  __  ___ _   _ _     _____ ____                      _     
 |  \/  | | | | |   | ____/ ___|  ___  __ _ _ __ ___| |__  
 | |\/| | | | | |   |  _| \___ \ / _ \/ _` | '__/ __| '_ \ 
 | |  | | |_| | |___| |___ ___) |  __/ (_| | | | (__| | | |
 |_|  |_|\___/|_____|_____|____/ \___|\__,_|_|  \___|_| |_|
🚀 MULESEARCH - TELEGRAM OSINT TOOL AVANZADO v3.0
""")
        print("=" * 70)
        print("💡 FORMATOS VÁLIDOS: @usuario, +34123456789, nombre apellido")
        print("=" * 70)
        target = input("Ingresa el username, número de teléfono o nombre: ").strip()
        print("\n🎯 SELECCIONA EL TIPO DE ANÁLISIS:")
        print("1. 🔍 Búsqueda rápida (información básica)")
        print("2. 📊 Búsqueda completa (con historial y estadísticas)")
        print("3. 🕵️ Recuperación de fotos antiguas")
        print("4. 🚀 ANÁLISIS OSINT COMPLETO (NUEVO)")
        print("5. 📈 Análisis de patrones de comportamiento")
        print("6. 🌐 Búsqueda cross-platform")
        print("7. 📍 Análisis geográfico")
        print("8. 😊 Análisis de sentimiento")
        print("9. 👥 Buscar por nombre en grupos")
        print("10. 💬 ANÁLISIS DETALLADO DE MENSAJES (NUEVO)")
        print("11. 🔤 VOCABULARIO Y PALABRAS (NUEVO)")
        print("12. 🎯 TEMAS DE CONVERSACIÓN (NUEVO)")
        print("13. 📧 EXTRAER EMAILS DEL USUARIO (NUEVO)")
        print("14. 📞 EXTRAER TELÉFONOS (NUEVO)")
        print("15. 🕸️ MAPA DE CONEXIONES (NUEVO)")
        print("16. ✍️ ANÁLISIS DE ESCRITURA (NUEVO)")
        print("17. 🚀 REPORTE OSINT PREMIUM (NUEVO)")
        option = input("\nOpción (1-17): ").strip()

        if option == "1":
            print("🔍 Buscando información básica...")
            results = await osint_tool.get_user_info(target)
            if results:
                print(f"✅ Usuario encontrado: {results['first_name']} {results['last_name']}")
                print(f"📱 Teléfono: {results['phone']}")
                print(f"🌐 Username: @{results['username']}")
                print(f"🆔 ID: {results['id']}")
                print(f"📝 Biografía: {results['bio']}")
            else:
                print("❌ No se pudo obtener información del usuario")
                print("\n💡 SUGERENCIAS:")
                print("   - Usa el formato @usuario (ej: @usuario)")
                print("   - Para números, incluye código de país (ej: +34123456789)")
                print("   - Verifica que el usuario existe y no es privado")

        elif option == "2":
            print("🔍 Realizando búsqueda completa...")
            results = await osint_tool.get_full_user_info(target)
            if results:
                filename = osint_tool.save_results(results)
                report = osint_tool.generate_report(results)
                print(report)
                print(f"\n✅ Información completa guardada en: {filename}")
            else:
                print("❌ No se pudo obtener información del usuario")

        elif option == "3":
            print("🕵️ Iniciando recuperación de fotos...")
            try:
                profile_photos = await osint_tool.get_profile_photos(target)
                print(f"   Fotos de perfil: {len(profile_photos) if profile_photos else 0}")
                public_photos = await osint_tool.search_public_photos(target)
                print(f"   Fotos públicas: {len(public_photos) if public_photos else 0}")
            except Exception as e:
                print(f"❌ Error en depuración: {e}")

        elif option == "4":
            print("🚀 Iniciando análisis OSINT completo...")
            results = await osint_tool.get_complete_osint_report(target)
            if not results and ' ' in target and not target.startswith('@'):
                print("🔍 Intentando con formato @username...")
                suggested_username = f"@{target.replace(' ', '').lower()}"
                results = await osint_tool.get_complete_osint_report(suggested_username)
            if results:
                filename = osint_tool.save_results(results)
                report = osint_tool.generate_advanced_report(results)
                print(report)
                print(f"\n✅ Reporte completo guardado en: {filename}")
            else:
                print("❌ No se pudo obtener información del usuario")
                print("\n💡 SUGERENCIAS:")
                print("   - Asegúrate de que el usuario existe en Telegram")
                print("   - Usa el formato @usuario (ej: @marysan)")
                print("   - Si es un número, incluye el código de país")
                print("   - Verifica que el usuario no sea privado")
                print("   - Prueba la opción 9 para buscar por nombre en grupos")

        elif option == "5":
            print("📈 Analizando patrones de comportamiento...")
            patterns = await osint_tool.analyze_message_patterns(target)
            if patterns:
                print(f"📊 Mensajes procesados: {patterns.get('total_messages_processed', 0)}")
                print(f"📝 Mensajes con texto: {patterns.get('messages_with_text', 0)}")
                if patterns.get('most_active_hour'):
                    hour, count = patterns['most_active_hour']
                    print(f"🕒 Hora más activa: {hour:02d}:00 ({count} mensajes)")
                else:
                    print(f"🕒 Hora más activa: No disponible")
                print(f"📏 Longitud promedio: {patterns.get('avg_message_length', 0):.1f} caracteres")
                print(f"🖼️ Total de medios: {patterns.get('total_media', 0)}")
                print(f"↩️ Respuestas: {patterns.get('reply_frequency', 0)}")
                print(f"🔄 Forwards: {patterns.get('forward_frequency', 0)}")
                print("🔤 Palabras más usadas:")
                for word, count in patterns.get('most_common_words', [])[:5]:
                    print(f"   - '{word}' ({count} veces)")
            else:
                print("❌ No se pudieron analizar los patrones")

        elif option == "6":
            print("🌐 Buscando en otras plataformas...")
            user_info = await osint_tool.get_user_info(target)
            username = user_info['username'] if user_info and user_info.get('username') != 'N/A' else (target if not target.isdigit() and ' ' not in target else None)
            if username:
                platforms = await osint_tool.search_username_across_platforms(username)
                print("\n📊 RESULTADOS:")
                print("-" * 50)
                found_count = 0
                for platform, data in platforms.items():
                    status = "✅ ENCONTRADO" if data.get('exists') else "❌ NO ENCONTRADO"
                    if data.get('exists'):
                        found_count += 1
                    print(f"   {platform.upper():<12} {status}")
                    if data.get('exists'):
                        print(f"   📎 URL: {data['url']}")
                    print()
                print(f"📈 Total encontrados: {found_count}/{len(platforms)} plataformas")
            else:
                print("❌ No se pudo obtener un username válido")

        elif option == "7":
            print("📍 Analizando menciones geográficas...")
            locations = await osint_tool.geolocation_analysis(target)
            if locations and locations['mentioned_locations']:
                print(f"📍 Ubicaciones mencionadas: {', '.join(locations['mentioned_locations'][:10])}")
                print(f"📊 Total de menciones: {locations['total_mentions']}")
                print(f"🏙️ Ubicaciones únicas: {locations['unique_locations']}")
            else:
                print("❌ No se encontraron menciones geográficas")

        elif option == "8":
            print("😊 Analizando sentimiento...")
            sentiment = await osint_tool.sentiment_analysis(target)
            if sentiment:
                print(f"😊 Positivo: {sentiment.get('positive_percentage', 0):.1f}%")
                print(f"😞 Negativo: {sentiment.get('negative_percentage', 0):.1f}%")
                print(f"😐 Neutral: {sentiment.get('neutral_percentage', 0):.1f}%")
                print(f"📊 Total mensajes analizados: {sentiment.get('total_messages', 0)}")
            else:
                print("❌ No se pudo realizar el análisis de sentimiento")

        elif option == "9":
            print("👥 Buscando por nombre en grupos...")
            if ' ' in target:
                users_found = await osint_tool.search_user_by_name(target)
                if users_found:
                    print(f"\n✅ Se encontraron {len(users_found)} usuarios:")
                    for i, user in enumerate(users_found, 1):
                        print(f"\n{i}. 👤 {user['first_name']} {user['last_name']}")
                        print(f"   🌐 Username: @{user['username']}")
                        print(f"   🆔 ID: {user['id']}")
                        print(f"   📍 Encontrado en: {user['found_in']} ({user['chat_type']})")
                    choice = input("\n🔢 ¿Quieres analizar alguno? Ingresa el número (o Enter para salir): ").strip()
                    if choice and choice.isdigit() and 1 <= int(choice) <= len(users_found):
                        user = users_found[int(choice) - 1]
                        if user['username'] != 'N/A':
                            print(f"\n🚀 Analizando @{user['username']}...")
                            results = await osint_tool.get_complete_osint_report(user['username'])
                            if results:
                                filename = osint_tool.save_results(results)
                                report = osint_tool.generate_advanced_report(results)
                                print(report)
                                print(f"\n✅ Reporte completo guardado en: {filename}")
                        else:
                            print("❌ Este usuario no tiene username público")
                else:
                    print("❌ No se encontraron usuarios con ese nombre")
            else:
                print("❌ Ingresa un nombre completo (con apellido) para buscar")

        elif option == "10":
            print("💬 Obteniendo análisis detallado de mensajes...")
            enhanced_report = await osint_tool.get_enhanced_osint_report(target)
            if enhanced_report:
                filename = osint_tool.save_results(enhanced_report)
                detailed_report = osint_tool.generate_detailed_message_report(enhanced_report)
                print(detailed_report)
                print(f"\n✅ Reporte detallado guardado en: {filename}")
                if enhanced_report.get('full_messages'):
                    view_more = input("\n¿Ver más mensajes? (s/n): ").strip().lower()
                    if view_more == 's':
                        messages = enhanced_report['full_messages']
                        print(f"\n📨 MOSTRANDO MENSAJES ({len(messages)} total):")
                        for i, msg in enumerate(messages, 1):
                            print(f"\n{i}. 📅 {msg['date']}")
                            if msg.get('text'):
                                print(f"   💬 {msg['text']}")
                            if msg.get('media_type') and msg['media_type'] != 'text':
                                print(f"   📎 {msg['media_type']}")
                            print("   " + "="*60)
                            if i % 10 == 0 and i < len(messages):
                                cont = input("\n¿Continuar? (s/n): ").strip().lower()
                                if cont != 's':
                                    break
            else:
                print("❌ No se pudo obtener información del usuario")

        elif option == "11":
            print("🔤 Analizando vocabulario y palabras utilizadas...")
            word_analysis = await osint_tool.get_all_words_used(target, limit=300)
            if word_analysis:
                print(f"\n📊 ESTADÍSTICAS DE VOCABULARIO:")
                print(f"Palabras únicas utilizadas: {word_analysis.get('total_unique_words', 0)}")
                print(f"\n📈 PALABRAS MÁS USADAS (Top 30):")
                for i, (word, count) in enumerate(word_analysis.get('most_common_words', [])[:30], 1):
                    print(f"{i:2d}. '{word}': {count} veces")
                total_words = sum(word_analysis.get('word_frequency', {}).values())
                if total_words > 0:
                    diversity = (word_analysis.get('total_unique_words', 0) / total_words) * 100
                    print(f"\n🎯 DIVERSIDAD LÉXICA: {diversity:.1f}% (palabras únicas/totales)")
            else:
                print("❌ No se pudo analizar el vocabulario")

        elif option == "12":
            print("🎯 Identificando temas de conversación...")
            topics_analysis = await osint_tool.get_conversation_topics(target, limit=300)
            if topics_analysis:
                print(f"\n🏷️ TEMAS IDENTIFICADOS:")
                for topic, count in topics_analysis.get('topic_counts', {}).items():
                    if count > 0:
                        print(f"   📌 {topic}: {count} mensajes")
                common_topics = topics_analysis.get('most_common_topics', [])
                if common_topics:
                    print(f"\n🎯 TEMAS PRINCIPALES: {', '.join(common_topics)}")
                    view_examples = input("\n¿Ver ejemplos de mensajes por tema? (s/n): ").strip().lower()
                    if view_examples == 's':
                        topic_messages = topics_analysis.get('topic_messages', {})
                        for topic in common_topics[:3]:
                            messages = topic_messages.get(topic, [])
                            if messages:
                                print(f"\n📝 EJEMPLOS DE '{topic.upper()}':")
                                for i, msg in enumerate(messages[:3], 1):
                                    print(f"   {i}. {msg['date']}: {msg['text']}")
            else:
                print("❌ No se pudieron identificar temas")

        elif option == "13":
            print(f"📧 Extrayendo emails del usuario: {target}")
            try:
                input_info = osint_tool.validate_telegram_input(target)
                if input_info['type'] == 'name':
                    users_found = await osint_tool.search_user_by_name(target)
                    if not users_found:
                        print("❌ No se encontró ningún usuario con ese nombre.")
                    elif len(users_found) == 1 and users_found[0]['username'] != 'N/A':
                        entity = await osint_tool.client.get_entity(users_found[0]['username'])
                        all_emails = await osint_tool.extract_emails_from_entity(entity, limit=2000)
                    else:
                        entity = await osint_tool.client.get_entity(target)
                        all_emails = await osint_tool.extract_emails_from_entity(entity, limit=2000)
                else:
                    entity = await osint_tool.client.get_entity(target)
                    all_emails = await osint_tool.extract_emails_from_entity(entity, limit=2000)

                if all_emails:
                    seen = set()
                    unique_emails = []
                    for item in all_emails:
                        if item['email'] not in seen:
                            unique_emails.append(item)
                            seen.add(item['email'])
                    await osint_tool.save_emails_to_csv(unique_emails, filename=f'emails_{target.replace("@", "").replace(" ", "_")}.csv')
                    print(f"\n✅ Emails encontrados en {target}:")
                    for item in unique_emails[:10]:
                        print(f"📧 {item['email']}")
                    if len(unique_emails) > 10:
                        print(f"... y {len(unique_emails) - 10} más")
                else:
                    print("❌ No se encontraron emails en los mensajes de este usuario.")
            except Exception as e:
                logger.error(f"Error extrayendo emails: {e}")
                print(f"❌ Error: {e}")

        elif option == "14":
            print("📞 Extrayendo números de teléfono mencionados...")
            phones = await osint_tool.extract_phone_numbers(target, limit=300)
            if phones:
                print(f"\n✅ Números de teléfono encontrados ({len(phones)}):")
                for i, phone in enumerate(phones[:10], 1):
                    print(f"{i}. 📞 {phone['phone']}")
                    print(f"   📅 {phone['date']}")
                    print(f"   💬 Contexto: {phone['context']}")
                    print()
                if len(phones) > 10:
                    print(f"... y {len(phones) - 10} más")
                
                save = input("¿Guardar resultados? (s/n): ").strip().lower()
                if save == 's':
                    filename = f"phones_{target.replace('@', '').replace(' ', '_')}.json"
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump(phones, f, indent=2, ensure_ascii=False)
                    print(f"💾 Resultados guardados en: {filename}")
            else:
                print("❌ No se encontraron números de teléfono")

        elif option == "15":
            print("🕸️ Creando mapa de conexiones...")
            connections = await osint_tool.get_user_connections_map(target)
            if connections and connections.get('common_groups'):
                print(f"\n✅ Mapa de conexiones para {target}:")
                print(f"Grupos en común: {len(connections['common_groups'])}")
                
                for i, group in enumerate(connections['common_groups'][:5], 1):
                    print(f"\n{i}. 🏷️ {group['name']} ({group['type']})")
                    print(f"   👥 {group['participants_count']} miembros")
                    print(f"   🤝 {len(group['common_contacts'])} contactos en común")
                    
                    if group['common_contacts']:
                        print("   Contactos destacados:")
                        for contact in group['common_contacts'][:3]:
                            name = f"{contact['first_name']} {contact['last_name']}".strip()
                            print(f"     • {name} (@{contact['username']})")
            else:
                print("❌ No se pudieron obtener las conexiones")

        elif option == "16":
            print("✍️ Analizando estilo de escritura...")
            style_analysis = await osint_tool.analyze_message_style(target, limit=300)
            if style_analysis:
                print(f"\n📊 ANÁLISIS DE ESTILO DE ESCRITURA:")
                print(f"Longitud promedio: {style_analysis.get('avg_message_length', 0):.1f} caracteres")
                print(f"Mensajes analizados: {style_analysis.get('writing_style_metrics', {}).get('total_messages_analyzed', 0)}")
                
                punctuation = style_analysis.get('punctuation_usage', {})
                print(f"\n📝 USO DE PUNTUACIÓN:")
                print(f"  • Puntos: {punctuation.get('periods', 0)}")
                print(f"  • Comas: {punctuation.get('commas', 0)}")
                print(f"  • Exclamaciones: {punctuation.get('exclamations', 0)}")
                print(f"  • Preguntas: {punctuation.get('questions', 0)}")
                
                emoticons = style_analysis.get('emoticon_usage', {})
                if emoticons:
                    print(f"\n😊 EMOTICONOS MÁS USADOS:")
                    for emoticon, count in emoticons.most_common(5):
                        print(f"  • {emoticon}: {count} veces")
                
                capitalization = style_analysis.get('capitalization_patterns', {})
                if capitalization.get('total_words', 0) > 0:
                    rate = capitalization.get('capitalization_rate', 0)
                    print(f"\n🔠 TASA DE CAPITALIZACIÓN: {rate:.1f}%")
            else:
                print("❌ No se pudo analizar el estilo de escritura")

        elif option == "17":
            print("🚀 Generando reporte OSINT PREMIUM...")
            premium_report = await osint_tool.get_premium_osint_report(target)
            if premium_report:
                filename = osint_tool.save_results(premium_report)
                report = osint_tool.generate_advanced_report(premium_report)
                print(report)
                print(f"\n✅ Reporte premium guardado en: {filename}")
                
                # Mostrar estadísticas adicionales
                print(f"\n📈 ESTADÍSTICAS PREMIUM:")
                print(f"• Emails extraídos: {len(premium_report.get('extracted_emails', []))}")
                print(f"• Teléfonos encontrados: {len(premium_report.get('extracted_phones', []))}")
                print(f"• Grupos analizados: {len(premium_report.get('public_groups', []))}")
                print(f"• Conexiones mapeadas: {len(premium_report.get('connections_map', {}).get('common_groups', []))}")
            else:
                print("❌ No se pudo generar el reporte premium")

        else:
            print("❌ Opción no válida")

    except Exception as e:
        logger.error(f"Error en la ejecución: {e}")
        print(f"❌ Error: {e}")
    finally:
        osint_tool.cleanup_temp_files()
        print("\n🧹 Limpieza completada")


if __name__ == "__main__":
    for folder in ['photos', 'deleted_photos']:
        if not os.path.exists(folder):
            os.makedirs(folder)
    asyncio.run(main())