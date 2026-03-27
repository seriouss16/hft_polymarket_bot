"""Async LSTM predictor with ONNX and Keras backends."""

import asyncio
import logging
import os

import numpy as np

_tf = None
_ort = None


def _get_tf():
    """Lazy-import TensorFlow to avoid 2-5s startup penalty when LSTM is off."""
    global _tf
    if _tf is None:
        import tensorflow as tf
        _tf = tf
    return _tf


def _get_ort():
    """Lazy-import ONNX Runtime on first use."""
    global _ort
    if _ort is None:
        import onnxruntime as ort
        _ort = ort
    return _ort


class AsyncLSTMPredictor:
    """Run LSTM inference off the event loop via a thread pool."""

    def __init__(self, model_path=None, history_len=100):
        """Initialize predictor; model is built lazily to defer heavy imports."""
        self.history_len = history_len
        self._executor = None
        self.model_path = model_path
        self.use_onnx = model_path is not None and os.path.exists(model_path)
        self.session = None
        self.input_name = None
        self.output_name = None
        self.model = None
        self.last_prediction = 0.0

        if self.use_onnx:
            ort = _get_ort()
            self.session = ort.InferenceSession(model_path)
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name

    @property
    def executor(self):
        """Create thread pool on first use so idle bots pay no cost."""
        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self._executor = ThreadPoolExecutor(max_workers=1)
        return self._executor

    def _build_model(self):
        """Construct a minimal Keras LSTM when no ONNX checkpoint is provided."""
        tf = _get_tf()
        model = tf.keras.Sequential([
            tf.keras.layers.Input(shape=(self.history_len, 1)),
            tf.keras.layers.LSTM(32, return_sequences=False),
            tf.keras.layers.Dense(1),
        ])
        model.compile(optimizer="adam", loss="mse")
        return model

    def _ensure_model(self):
        """Build Keras model on first predict call to defer the TF import."""
        if self.model is None and not self.use_onnx:
            self.model = self._build_model()

    def shutdown(self):
        """Release thread pool resources."""
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    async def predict(self, data_deque):
        """Return price forecast; runs inference in a thread to avoid blocking the loop."""
        if len(data_deque) < self.history_len:
            if data_deque:
                self.last_prediction = float(data_deque[-1])
            return self.last_prediction

        try:
            raw_data = list(data_deque)[-self.history_len:]

            clean_prices = []
            for item in raw_data:
                if isinstance(item, dict):
                    clean_prices.append(float(item["price"]))
                else:
                    clean_prices.append(float(item))

            input_data = np.array(clean_prices, dtype=np.float32).reshape(1, self.history_len, 1)

            if self.use_onnx:
                input_data_np = np.ascontiguousarray(input_data, dtype=np.float32)
                ort_inputs = {self.input_name: input_data_np}

                def _onnx_run():
                    return self.session.run([self.output_name], ort_inputs)

                loop = asyncio.get_running_loop()
                ort_outs = await loop.run_in_executor(self.executor, _onnx_run)
                prediction = ort_outs[0]
            else:
                self._ensure_model()
                loop = asyncio.get_running_loop()
                prediction = await loop.run_in_executor(
                    self.executor,
                    lambda: self.model.predict(input_data, verbose=0),
                )

            self.last_prediction = float(prediction[0][0])
            if not np.isfinite(self.last_prediction):
                self.last_prediction = float(clean_prices[-1])
            return self.last_prediction

        except Exception as exc:
            logging.warning("LSTM predict failed, falling back to last price: %s", exc)
            if data_deque:
                self.last_prediction = float(data_deque[-1])
            return self.last_prediction
