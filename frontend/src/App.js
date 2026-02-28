import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import YouTube from 'react-youtube';
import './App.css';

// 1. Updated Languages Array with Flag Image URLs
const languages = [
  { code: 'en', name: 'English', icon: 'https://flagcdn.com/w40/us.png' },
  { code: 'es', name: 'Spanish', icon: 'https://flagcdn.com/w40/es.png' },
  { code: 'fr', name: 'French', icon: 'https://flagcdn.com/w40/fr.png' },
  { code: 'de', name: 'German', icon: 'https://flagcdn.com/w40/de.png' },
];

// 2. Custom Dropdown Component to handle images
const CustomSelect = ({ value, onChange, options }) => {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find(opt => opt.code === value);

  return (
    <div 
      className="custom-select-container" 
      tabIndex={0} 
      onBlur={() => setIsOpen(false)}
    >
      <div className="custom-select-trigger" onClick={() => setIsOpen(!isOpen)}>
        <img src={selectedOption.icon} alt={selectedOption.name} className="flag-icon" />
        <svg className="chevron" viewBox="0 0 24 24" width="18" height="18">
          <path d="M7 10l5 5 5-5z" fill="#333"/>
        </svg>
      </div>
      {isOpen && (
        <div className="custom-select-dropdown">
          {options.map(opt => (
            <div 
              key={opt.code} 
              className="custom-select-option"
              onMouseDown={() => {
                onChange(opt.code);
                setIsOpen(false);
              }}
            >
              <img src={opt.icon} alt={opt.name} className="flag-icon" />
              <span>{opt.name}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

function App() {
  const [url, setUrl] = useState('');
  const [translated_transcript, setTranslatedTranscript] = useState([]);
  const [transcript, setTranscript] = useState([]);
  const [videoId, setVideoId] = useState('');
  const [player, setPlayer] = useState(null);
  
  // TRACKING STATE
  const [currentLineIndex, setCurrentLineIndex] = useState(0);
  const [userInput, setUserInput] = useState('');
  const [showInput, setShowInput] = useState(false);  
  const [fromLang, setFromLang] = useState('en'); // Default to English
  const [toLang, setToLang] = useState('es');   // Default to Spanish

  const inputRef = useRef(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    try {
      const response = await axios.post('http://127.0.0.1:5000/api/transcript', { 
        url,
        from_lang: fromLang,
        to_lang: toLang
       });
      setTranscript(response.data.snippets); 
      setTranslatedTranscript(response.data.translated_snippets);
      setVideoId(response.data.video_id);
      setCurrentLineIndex(0); // Reset to start
      setShowInput(false);
    } catch (error) {
      console.error("Error:", error);
    }
  };

  // ---------------------------------------------------------
  // THE BRAKE PEDAL (Auto-Pause Logic)
  // ---------------------------------------------------------
  useEffect(() => {
    let interval;
    if (player && transcript.length > 0) {
      interval = setInterval(async () => {
        // We only check time if we are NOT already waiting for user input
        if (!showInput) {
          const currentTime = await player.getCurrentTime();
          const currentLine = transcript[currentLineIndex];
          const endTime = 
          currentLineIndex < transcript.length - 1 && currentLine.start + currentLine.duration > transcript[currentLineIndex + 1].start 
          ? transcript[currentLineIndex + 1].start-.2 
          : Math.min(currentLine.start + currentLine.duration, player.getDuration());
            // If we reached the end of the line...
          if (currentTime >= endTime) {
            player.pauseVideo();   // 1. Pause Video
            setShowInput(true);    // 2. Show Input Box
          }
        }
      }, 100); // Check faster (every 0.1s) for better precision
    }
    return () => clearInterval(interval);
  }, [player, transcript, currentLineIndex, showInput]);

  // ---------------------------------------------------------
  // THE GAS PEDAL (Go to next line)
  // ---------------------------------------------------------
  const handleInputSubmit = (e) => {
    if (e.key === 'Enter') {
      // Move to next line if available
      if (currentLineIndex < transcript.length - 1) {
        const nextIndex = currentLineIndex + 1;
        setCurrentLineIndex(nextIndex);
        setUserInput('');       // Clear text
        setShowInput(false);    // Hide box
        
        player.playVideo();     // Resume Video
      } else {
        alert("You finished the video!");
      }
    }
  };

  // Auto-focus the input box when it appears
  useEffect(() => {
    if (showInput && inputRef.current) {
      inputRef.current.focus();
    }
  }, [showInput]);

  // ---------------------------------------------------------
  // RENDER
  // ---------------------------------------------------------
  return (
    <div className="App">
      <header className="App-header">
        
        {/* LANDING PAGE UI */}
        {!videoId && (
          <div className="landing-container">
            <h1 className="landing-title">Vidioma</h1>
            
            <form onSubmit={handleSubmit} className="modern-search-bar">
              {/* Link Icon */}
              <div className="input-icon">
                <svg viewBox="0 0 24 24" width="20" height="20" fill="#888">
                  <path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/>
                </svg>
              </div>

              {/* URL Input */}
              <input 
                type="text" 
                placeholder="Paste YouTube URL..." 
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                required
              />

              {/* Vertical Divider */}
              <div className="divider"></div>

              {/* Language Selectors */}
              <div className="landing-lang-group">
                <CustomSelect 
                  value={fromLang} 
                  onChange={setFromLang} 
                  options={languages} 
                />

                <span className="lang-arrow">→</span>

                <CustomSelect 
                  value={toLang} 
                  onChange={setToLang} 
                  options={languages} 
                />
              </div>

              {/* Submit Button */}
              <button type="submit" className="go-button">GO</button>
            </form>
          </div>
        )}

        {/* TRANSCRIPT & VIDEO UI (Hides title and search bar when active) */}
        {videoId && (
          <div className="content-area">
            {/* Video Player */}
            <div className="video-section">
              <div className="video-wrapper">
                <YouTube 
                  videoId={videoId} 
                  opts={{ 
                    height: '390', 
                    width: '640',
                    playerVars: {
                      rel: 0, 
                      modestbranding: 1, 
                      autoplay: 1, 
                    }
                  }}
                  onReady={(event) => setPlayer(event.target)}
                />
                {showInput && <div className="video-curtain" />}
              </div>
            </div>

            {/* Focus Mode Display */}
            {transcript.length > 0 && (
              <div className="focus-card">
                <h2 className="current-text">
                  {transcript[currentLineIndex].source}
                </h2>
                <h2 className="current-text">
                  {translated_transcript[currentLineIndex]}
                </h2>

                {showInput ? (
                  <div className="input-container">
                    <input 
                      ref={inputRef}
                      type="text" 
                      className="big-input"
                      placeholder="Type translation..." 
                      value={userInput}
                      onChange={(e) => setUserInput(e.target.value)}
                      onKeyDown={handleInputSubmit}
                    />
                    <p className="hint">Press Enter to continue</p>
                  </div>
                ) : (
                  <p className="listening-indicator">👂 Listening...</p>
                )}

                <div className="progress">
                  Line {currentLineIndex + 1} of {transcript.length}
                </div>
              </div>
            )}
          </div>
        )}
      </header>
    </div>
  );
}

export default App;