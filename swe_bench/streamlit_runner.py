import sys
import subprocess
import os
import time
import psutil
import argparse

class StreamlitRunner:
    def __init__(self):
        self.process = None

    def run(self, output_dir):
        # Get the absolute path to streamlit_dashboard.py
        dashboard_path = os.path.join(os.path.dirname(__file__), "streamlit_dashboard.py")
        # Start Streamlit as a separate process
        cmd = [sys.executable, "-m", "streamlit", "run", dashboard_path,
               "--", "--output-dir", output_dir]

        self.process = subprocess.Popen(cmd)

    def stop(self):
        if self.process:
            try:
                # Get the process group
                parent = psutil.Process(self.process.pid)
                children = parent.children(recursive=True)
                
                # Terminate children first
                for child in children:
                    child.terminate()
                
                # Terminate parent
                parent.terminate()
                
                # Wait for processes to terminate
                gone, alive = psutil.wait_procs(children + [parent], timeout=3)
                
                # Force kill if still alive
                for p in alive:
                    p.kill()
                    
            except psutil.NoSuchProcess:
                pass
            finally:
                self.process = None

def main():
    parser = argparse.ArgumentParser(description='Run Streamlit dashboard for SWE Bench visualization')
    parser.add_argument('--output-dir', default=os.path.expanduser('~/swe/outputs'), 
                      help='Directory containing the output files (default: ~/swe/outputs)')
    args = parser.parse_args()

    # Convert to absolute path and create if needed
    output_dir = os.path.abspath(os.path.expanduser(args.output_dir))
    os.makedirs(output_dir, exist_ok=True)
        
    # Start the Streamlit dashboard
    runner = StreamlitRunner()
    runner.run(output_dir)
    
    try:
        print(f"Streamlit dashboard started. Monitoring directory: {output_dir}")
        print("Press Ctrl+C to stop...")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping Streamlit dashboard...")
        runner.stop()
        print("Streamlit dashboard stopped.")

if __name__ == "__main__":
    main()
