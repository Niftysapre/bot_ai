import requests
import time
import threading
import queue
from concurrent.futures import ThreadPoolExecutor


class OllamaHandler:
    _instance = None
    _lock = threading.Lock()
    _request_queue = queue.Queue()
    _is_processing = False
    _max_workers = 3  # Максимальное количество одновременных запросов

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(OllamaHandler, cls).__new__(cls)
                cls._instance._initialized = False
                cls._start_queue_processor()
            return cls._instance

    def __init__(self, model="gemma2:2b"):
        if self._initialized:
            return

        self.model = model
        self.api_url = "http://localhost:11434/api/generate"
        self.system_prompt = """
        Ты - помощник службы поддержки на русском языке. Твоя задача:
        1. Давать краткие и четкие ответы
        2. Если не знаешь точного ответа, честно сказать об этом
        3. Отвечать по существу заданного вопроса
        """
        self.executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._initialized = True

    @classmethod
    def _start_queue_processor(cls):
        """Запускает обработчик очереди в отдельном потоке"""

        def process_queue():
            while True:
                try:
                    # Получаем задачу из очереди (блокирует поток до получения задачи)
                    task, callback = cls._request_queue.get(block=True)
                    prompt, model = task

                    # Обрабатываем задачу
                    result = cls._instance._make_request(prompt, model)

                    # Вызываем callback с результатом
                    if callback:
                        callback(result)

                    # Отмечаем задачу как выполненную
                    cls._request_queue.task_done()

                    # Небольшая пауза между запросами
                    time.sleep(0.1)
                except Exception as e:
                    print(f"Ошибка в обработчике очереди: {e}")

        # Запускаем обработчик очереди в отдельном потоке
        queue_thread = threading.Thread(target=process_queue, daemon=True)
        queue_thread.start()

    def _make_request(self, prompt, model=None):
        """Выполняет фактический запрос к API Ollama"""
        if model is None:
            model = self.model

        try:
            full_prompt = f"{self.system_prompt}\nВопрос: {prompt}\nОтвет:"
            response = requests.post(
                self.api_url,
                json={
                    "model": model,
                    "prompt": full_prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.7,
                        "top_k": 40,
                        "top_p": 0.9,
                    }
                },
                timeout=30  # Добавляем таймаут
            )

            if response.status_code == 200:
                return response.json().get("response")

            print(f"Ошибка API Ollama: статус {response.status_code}, ответ: {response.text}")
            return None

        except requests.exceptions.Timeout:
            print("Превышено время ожидания ответа от Ollama")
            return None
        except requests.exceptions.ConnectionError:
            print("Ошибка соединения с Ollama API")
            return None
        except Exception as e:
            print(f"Ошибка при генерации ответа: {e}")
            return None

    def generate_response(self, prompt, callback=None):
        """
        Добавляет запрос в очередь и возвращает результат

        Args:
            prompt: Текст запроса
            callback: Функция обратного вызова для асинхронной обработки

        Returns:
            Текст ответа при синхронном вызове или None при асинхронном
        """
        if callback:
            # Асинхронный режим: добавляем в очередь и возвращаем None
            self._request_queue.put(((prompt, self.model), callback))
            return None
        else:
            # Синхронный режим: непосредственно выполняем запрос и возвращаем результат
            return self._make_request(prompt, self.model)

    def async_generate_response(self, prompt):
        """
        Асинхронная генерация ответа через ThreadPoolExecutor

        Args:
            prompt: Текст запроса

        Returns:
            Future object, из которого можно получить результат через .result()
        """
        return self.executor.submit(self._make_request, prompt, self.model)