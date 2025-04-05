from pathlib import Path
import yaml

class Config:
    def __init__(self):
        self.messages = self.load_messages()  # Теперь messages будет доступен как атрибут

    def load_messages(self):
        current_dir = Path(__file__).parent
        file_path = current_dir / "messages.yaml"
        
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                return yaml.safe_load(file)
        except Exception as e:
            raise RuntimeError(f"Ошибка загрузки конфига: {e}")

# Создаем экземпляр конфига при загрузке модуля
config = Config()