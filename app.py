import streamlit as st

st.set_page_config(page_title="Hackathon Base", layout="wide")
st.title("🔥 Hackathon Engine Ready")
st.write("Среда настроена, библиотеки подключены!")

user_input = st.text_input("Введи тестовый текст:")
if user_input:
    st.success(f"Получено: {user_input}")