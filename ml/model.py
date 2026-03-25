import numpy as np
import tensorflow as tf
import onnxruntime as ort
from concurrent.futures import ThreadPoolExecutor
import asyncio
import os

class AsyncLSTMPredictor:
    def __init__(self, model_path=None, history_len=100):
        self.history_len = history_len
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.model_path = model_path
        self.use_onnx = model_path is not None and os.path.exists(model_path)
        if self.use_onnx:
            # Load ONNX model for faster inference
            self.session = ort.InferenceSession(model_path)
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name
        else:
            # Если модели нет, создаем базовую структуру из bot3.py
            self.model = self._build_model()
        self.last_prediction = 0.0

    def _build_model(self):
        model = tf.keras.Sequential([
            tf.keras.layers.Input(shape=(self.history_len, 1)),
            tf.keras.layers.LSTM(32, return_sequences=False),
            tf.keras.layers.Dense(1)
        ])
        model.compile(optimizer='adam', loss='mse')
        return model

    async def predict(self, data_deque):
        if len(data_deque) < self.history_len:
            if data_deque:
                self.last_prediction = float(data_deque[-1])
            return self.last_prediction
        
        try:
            # ИЗВЛЕКАЕМ ТОЛЬКО ЦЕНЫ (фильтруем словари)
            # Если в очереди лежат словари, берем поле 'price'
            raw_data = list(data_deque)[-self.history_len:]
            
            clean_prices = []
            for item in raw_data:
                if isinstance(item, dict):
                    clean_prices.append(float(item['price']))
                else:
                    clean_prices.append(float(item))

            # Подготовка для Keras (превращаем в numpy array)
            input_data = np.array(clean_prices, dtype=np.float32).reshape(1, self.history_len, 1)

            if self.use_onnx:
                # ONNX Runtime inference
                input_data_np = np.ascontiguousarray(input_data, dtype=np.float32)
                ort_inputs = {self.input_name: input_data_np}
                ort_outs = self.session.run([self.output_name], ort_inputs)
                prediction = ort_outs[0]
            else:
                # Запуск предсказания в отдельном потоке.
                loop = asyncio.get_event_loop()
                prediction = await loop.run_in_executor(
                    self.executor,
                    lambda: self.model.predict(input_data, verbose=0),
                )

            self.last_prediction = float(prediction[0][0])
            if not np.isfinite(self.last_prediction):
                self.last_prediction = float(clean_prices[-1])
            return self.last_prediction

        except Exception as e:
            # На старте не роняем торговый цикл, используем последнюю доступную цену.
            if data_deque:
                self.last_prediction = float(data_deque[-1])
            return self.last_prediction