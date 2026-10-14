import React, { useState, useEffect } from 'react';

const TabControl = () => {
  const [activeTab, setActiveTab] = useState('ton_onoff');
  const [isAudioOn, setIsAudioOn] = useState(false);

  useEffect(() => {
    setActiveTab('ton_onoff');
  }, []);

  const handleTabChange = (tabName) => {
    setActiveTab(tabName);
  };

  const toggleAudio = () => {
    setIsAudioOn(!isAudioOn);
    // Hier können Sie die tatsächliche Audio-Logik implementieren
  };

  return (
    <div className="tab-container">
      <div className="tab-buttons">
        <button 
          className={`tab-button ${activeTab === 'ton_onoff' ? 'active' : ''}`}
          onClick={() => handleTabChange('ton_onoff')}
        >
          Ton {isAudioOn ? 'Ein' : 'Aus'}
        </button>
      </div>
      <div className="tab-content">
        {activeTab === 'ton_onoff' && (
          <div className="audio-control">
            <button 
              className={`audio-toggle ${isAudioOn ? 'on' : 'off'}`}
              onClick={toggleAudio}
            >
              {isAudioOn ? 'Ton ausschalten' : 'Ton einschalten'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default TabControl;
