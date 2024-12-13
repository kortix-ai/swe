import streamlit as st
import pandas as pd
import numpy as np

# Set page config
st.set_page_config(
    page_title="My Streamlit App",
    page_icon="📊",
    layout="wide"
)

# Add a title
st.title("Welcome to My Streamlit App! 👋")

# Add a sidebar
with st.sidebar:
    st.header("Settings")
    user_name = st.text_input("Enter your name", "Guest")
    color = st.color_picker("Pick a color", "#00ff00")

# Main content
st.header(f"Hello, {user_name}!")

# Create two columns
col1, col2 = st.columns(2)

with col1:
    st.subheader("Interactive Elements")
    number = st.slider("Select a number", 0, 100, 50)
    st.write(f"Selected number: {number}")
    
    if st.button("Generate Random Data"):
        chart_data = pd.DataFrame(
            np.random.randn(20, 3),
            columns=['A', 'B', 'C']
        )
        st.line_chart(chart_data)

with col2:
    st.subheader("File Upload")
    uploaded_file = st.file_uploader("Choose a CSV file", type="csv")
    if uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        st.write(df)
    else:
        st.info("Please upload a CSV file to see the data")

# Footer
st.markdown("---")
st.markdown("Built with ❤️ using Streamlit")
